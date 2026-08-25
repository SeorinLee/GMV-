"""Performance benchmark harness (spec §12).

Measures the metrics the spec asks for — total time, per-creator mean/median/P95, success
rate, browser restarts, page reloads — and lets you compare the NEW pipeline against a
legacy-style run *on the same account* so the "기존 대비" comparison uses real numbers.

Toggles that model the legacy behavior:
  dedupe=False           -> search every row (legacy did not dedupe)
  per_creator_restart=True -> new browser per creator (legacy `easy` reopened context)

The metric math (`summarize`) is pure and unit-tested; the live run needs a logged-in
session and is therefore not exercised in CI.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from gmv.models import RowStatus

# time.perf_counter is injected so tests are deterministic and the workflow ban on
# Date.now()/random does not apply here (this is a normal runtime module).


@dataclass
class BenchMetrics:
    label: str
    total_rows: int
    unique_creators: int
    searched: int
    total_seconds: float
    mean_seconds: float
    median_seconds: float
    p95_seconds: float
    success_rate: float
    failed: int
    browser_restarts: int
    page_reloads: int

    def as_dict(self) -> dict:
        return asdict(self)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = pct / 100 * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def summarize(
    label: str,
    durations: list[float],
    *,
    total_rows: int,
    unique_creators: int,
    statuses: list[RowStatus],
    total_seconds: float,
    browser_restarts: int,
    page_reloads: int,
) -> BenchMetrics:
    ok = sum(1 for s in statuses if s in (RowStatus.SUCCESS, RowStatus.RANGE))
    searched = len(durations)
    return BenchMetrics(
        label=label,
        total_rows=total_rows,
        unique_creators=unique_creators,
        searched=searched,
        total_seconds=round(total_seconds, 3),
        mean_seconds=round(sum(durations) / searched, 3) if searched else 0.0,
        median_seconds=round(_percentile(durations, 50), 3),
        p95_seconds=round(_percentile(durations, 95), 3),
        success_rate=round(ok / searched, 3) if searched else 0.0,
        failed=searched - ok,
        browser_restarts=browser_restarts,
        page_reloads=page_reloads,
    )


async def run_benchmark(
    usernames: list[str],
    session_factory,
    profile,
    *,
    label: str = "new",
    dedupe: bool = True,
    per_creator_restart: bool = False,
    clock=time.perf_counter,
) -> BenchMetrics:
    """Run the lookup pipeline while timing each creator (spec §12)."""
    total_rows = len(usernames)
    targets = list(dict.fromkeys(usernames)) if dedupe else list(usernames)
    unique = len(dict.fromkeys(usernames))

    durations: list[float] = []
    statuses: list[RowStatus] = []
    browser_restarts = 0

    started = clock()
    session = None
    try:
        if not per_creator_restart:
            session = session_factory(profile)
            await session.start()
            browser_restarts = 1

        for name in targets:
            if per_creator_restart:
                session = session_factory(profile)
                await session.start()
                browser_restarts += 1
            t0 = clock()
            res = await session.search_creator(name)
            durations.append(clock() - t0)
            statuses.append(res.status)
            if per_creator_restart:
                await session.close()
    finally:
        if session is not None and not per_creator_restart:
            await session.close()

    total_seconds = clock() - started
    return summarize(
        label,
        durations,
        total_rows=total_rows,
        unique_creators=unique,
        statuses=statuses,
        total_seconds=total_seconds,
        browser_restarts=browser_restarts,
        page_reloads=0 if not per_creator_restart else len(targets),
    )

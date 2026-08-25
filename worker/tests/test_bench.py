"""Benchmark harness tests (spec §12) — deterministic fake clock + session."""

import asyncio

from gmv.bench import _percentile, run_benchmark, summarize
from gmv.config import get_profile
from gmv.models import LookupResult, RowStatus


def test_percentile():
    vals = [1, 2, 3, 4, 5]
    assert _percentile(vals, 50) == 3
    assert _percentile([], 95) == 0.0
    assert _percentile([7], 95) == 7


def test_summarize_basic():
    m = summarize(
        "x",
        [1.0, 2.0, 3.0],
        total_rows=5,
        unique_creators=3,
        statuses=[RowStatus.SUCCESS, RowStatus.RANGE, RowStatus.FAILED],
        total_seconds=6.0,
        browser_restarts=1,
        page_reloads=0,
    )
    assert m.searched == 3
    assert m.mean_seconds == 2.0
    assert m.median_seconds == 2.0
    assert m.failed == 1
    assert m.success_rate == round(2 / 3, 3)


class _Clock:
    """Deterministic monotonic clock; each call advances by `step`."""

    def __init__(self, step=0.5):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


class FakeSession:
    def __init__(self, profile):
        self.profile = profile

    async def start(self):
        return None

    async def close(self):
        return None

    async def search_creator(self, name):
        return LookupResult(normalized_username=name, status=RowStatus.SUCCESS)


def test_dedupe_reduces_searched():
    names = ["a", "b", "a", "c", "b"]
    m = asyncio.run(
        run_benchmark(names, lambda p: FakeSession(p), get_profile("US_CHROME"),
                      dedupe=True, clock=_Clock())
    )
    assert m.total_rows == 5
    assert m.unique_creators == 3
    assert m.searched == 3            # deduped
    assert m.browser_restarts == 1    # single reused browser


def test_legacy_style_restarts_per_creator():
    names = ["a", "b", "c"]
    m = asyncio.run(
        run_benchmark(names, lambda p: FakeSession(p), get_profile("US_CHROME"),
                      label="legacy", dedupe=False, per_creator_restart=True, clock=_Clock())
    )
    assert m.searched == 3
    assert m.browser_restarts == 3    # one per creator (legacy behavior)
    assert m.page_reloads == 3

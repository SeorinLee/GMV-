"""Live performance comparison (spec §12) — run AFTER logging in.

    python -m tools.benchmark_live <xlsx_path> <US_CHROME|UK_EDGE> [--limit N]

Runs the NEW pipeline (dedupe + reused browser + event waits) and a LEGACY-STYLE run
(no dedupe + new browser per creator) against the SAME account, then prints real metrics
and the measured speed-up. Uses at most `--limit` unique creators (default 20, per spec §12).
"""

from __future__ import annotations

import argparse
import asyncio
import json

from gmv.api import real_session_factory
from gmv.bench import run_benchmark
from gmv.config import get_profile
from gmv.excel_engine import read_workbook


async def _main(xlsx: str, code: str, limit: int) -> None:
    plan = read_workbook(xlsx)
    names = [r.normalized_username for s in plan.sheets for r in s.rows if r.normalized_username]
    unique = list(dict.fromkeys(names))[:limit]
    profile = get_profile(code)

    new = await run_benchmark(unique, real_session_factory, profile, label="new", dedupe=True)
    legacy = await run_benchmark(
        unique, real_session_factory, profile, label="legacy",
        dedupe=False, per_creator_restart=True,
    )

    print(json.dumps({"new": new.as_dict(), "legacy": legacy.as_dict()}, indent=2, ensure_ascii=False))
    if legacy.total_seconds > 0:
        speedup = (legacy.total_seconds - new.total_seconds) / legacy.total_seconds * 100
        print(f"\n총 처리시간 단축: {speedup:.1f}%  (목표 50% 이상)")
        print(f"new={new.total_seconds}s  legacy={legacy.total_seconds}s  "
              f"per-creator new={new.mean_seconds}s legacy={legacy.mean_seconds}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("profile_code", choices=["US_CHROME", "UK_EDGE"])
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()
    asyncio.run(_main(args.xlsx, args.profile_code, args.limit))

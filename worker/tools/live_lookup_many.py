"""Run several production creator lookups in one logged-in browser session.

Usage:
    python tools/live_lookup_many.py US_CHROME creator1 creator2 creator3
"""

from __future__ import annotations

import asyncio
import sys
from time import perf_counter

from gmv.automation.session import TikTokAffiliateSession
from gmv.config import ProfileStatus, get_profile


async def main(profile_code: str, usernames: list[str]) -> int:
    session = TikTokAffiliateSession(get_profile(profile_code), headless=False)
    try:
        status = await session.start()
        print(f"[live-check] session={status.value}")
        if status is not ProfileStatus.CONNECTED:
            return 2

        for username in usernames:
            started = perf_counter()
            result = await session.search_creator(username)
            elapsed = perf_counter() - started
            gmv = result.gmv.value if result.gmv is not None else None
            error = getattr(result.error_code, "value", result.error_code)
            print(
                "[live-check] "
                f"creator={username} status={result.status.value} "
                f"gmv={gmv} items_sold={result.items_sold} "
                f"source={result.source} stage={result.current_stage} error={error} "
                f"message={result.error_message!r} seconds={elapsed:.2f}"
            )
        return 0
    finally:
        await session.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        raise SystemExit(
            "usage: python tools/live_lookup_many.py PROFILE_CODE USERNAME [USERNAME ...]"
        )
    raise SystemExit(asyncio.run(main(sys.argv[1], sys.argv[2:])))

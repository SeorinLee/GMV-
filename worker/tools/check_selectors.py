"""Live selector verification (spec §11, D10) — run AFTER logging in.

    python -m tools.check_selectors <US_CHROME|UK_EDGE> [search_term]

Opens the profile's Affiliate Center, navigates you to "Find creators", then reports which
fallback selector in each list actually matches the live DOM, and prints a snippet of the
first result row so you can confirm/correct `automation/selectors.py`. This turns selector
confirmation into a one-command task instead of guesswork.
"""

from __future__ import annotations

import asyncio
import sys

from gmv.automation import selectors
from gmv.config import get_profile


async def _probe(page, name: str, candidates: list[str]) -> None:
    print(f"\n[{name}]")
    for sel in candidates:
        try:
            count = await page.locator(sel).count()
            mark = "OK " if count > 0 else "   "
            print(f"  {mark}{count:>4}  {sel}")
        except Exception as exc:  # noqa: BLE001
            print(f"   ERR      {sel}  ({type(exc).__name__})")


async def _main(code: str, term: str) -> None:
    from playwright.async_api import async_playwright

    profile = get_profile(code)
    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=profile.storage_root, channel=profile.browser_channel, headless=False
    )
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(profile.affiliate_url, wait_until="domcontentloaded")

    print("브라우저에서 'Find creators' 화면으로 이동한 뒤 여기서 Enter를 누르세요...")
    await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)

    await _probe(page, "SEARCH_INPUT", selectors.SEARCH_INPUT)
    search = page.locator(", ".join(selectors.SEARCH_INPUT)).first
    try:
        await search.fill(term)
        await search.press("Enter")
        await page.wait_for_timeout(3000)
    except Exception as exc:  # noqa: BLE001
        print(f"검색 입력 실패: {exc}")

    await _probe(page, "RESULT_ROW", selectors.RESULT_ROW)
    await _probe(page, "RESULT_USERNAME", selectors.RESULT_USERNAME)
    await _probe(page, "GMV_CELL", selectors.GMV_CELL)
    await _probe(page, "NO_RESULT", selectors.NO_RESULT)

    row = page.locator(", ".join(selectors.RESULT_ROW)).first
    try:
        html = await row.evaluate("el => el.outerHTML")
        print("\n[first result row outerHTML — 첫 400자]\n", html[:400])
    except Exception:  # noqa: BLE001
        print("\n결과 행 HTML을 가져오지 못했습니다.")

    print("\n확인 후 브라우저를 닫으세요. Enter로 종료합니다...")
    await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)
    await context.close()
    await pw.stop()


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else "US_CHROME"
    term = sys.argv[2] if len(sys.argv) > 2 else "tayloraphotography"
    asyncio.run(_main(code, term))

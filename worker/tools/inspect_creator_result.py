"""Live DOM diagnostic for the creator search result + detail (spec: fix selectors safely).

Usage:
    python tools/inspect_creator_result.py DEFAULT arlynvibes

Reuses the existing persistent TikTok profile (you must already be logged in), opens the
creator search page, searches the username, and prints ONLY a bounded, safe summary of what the
DOM looks like so real selectors can be confirmed. Two screenshots are saved under diagnostics/.

NEVER printed/saved: full HTML, cookies, localStorage/sessionStorage, authorization headers,
URL query strings, or the browser profile path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from gmv.automation import selectors
from gmv.automation.session import (
    SEARCH_ICON_RELATIVE_SELECTORS,
    find_all_matching,
    find_first_visible,
)
from gmv.config import get_profile

MAX_ROW_TEXT = 500
DIAG_DIR = Path("diagnostics")


def _host_path(url: str) -> str:
    p = urlparse(url or "")
    return f"host={p.hostname} path={p.path}"


def _clip(text: str, limit: int = MAX_ROW_TEXT) -> str:
    t = " ".join((text or "").split())
    return t[:limit]


async def _texts_for(root, selector_list, limit_each: int = 120) -> list[str]:
    out: list[str] = []
    for sel in selector_list:
        try:
            for el in await root.locator(sel).all():
                if await el.is_visible():
                    txt = _clip((await el.inner_text()).strip(), limit_each)
                    if txt:
                        out.append(f"{sel} -> {txt}")
        except Exception as exc:  # noqa: BLE001
            out.append(f"{sel} -> <error: {type(exc).__name__}>")
    return out


async def inspect(code: str, username: str) -> None:
    from playwright.async_api import async_playwright

    profile = get_profile(code)
    DIAG_DIR.mkdir(parents=True, exist_ok=True)

    pw = await async_playwright().start()
    context = None
    try:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=profile.storage_root,
            channel=profile.browser_channel,
            headless=False,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto(profile.find_creators_url, wait_until="domcontentloaded")
        print(f"[find-creators] {_host_path(page.url)}")

        # --- search input ---
        matched_search = None
        for sel in selectors.SEARCH_INPUT:
            loc = page.locator(sel).first
            try:
                await loc.wait_for(state="visible", timeout=4000)
                matched_search = sel
                break
            except Exception:  # noqa: BLE001
                continue
        print(f"[search-input] matched selector: {matched_search!r}")
        if matched_search is None:
            print("  (no search input found — cannot continue)")
            return

        box = page.locator(matched_search).first
        search_dom = await box.evaluate(
            """el => {
                const attrs = node => ({
                    tag: node.tagName,
                    role: node.getAttribute('role'),
                    type: node.getAttribute('type'),
                    ariaLabel: node.getAttribute('aria-label'),
                    dataE2e: node.getAttribute('data-e2e'),
                    className: String(node.className || '').slice(0, 160),
                });
                const levels = [];
                let node = el;
                for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
                    levels.push({
                        depth,
                        self: attrs(node),
                        children: Array.from(node.children).slice(0, 12).map(attrs),
                    });
                }
                const suffix = el.parentElement?.querySelector('.core-input-group-suffix');
                return {
                    levels,
                    suffixDescendants: suffix
                        ? Array.from(suffix.querySelectorAll('*')).slice(0, 20).map(attrs)
                        : [],
                };
            }"""
        )
        print(f"[search-dom] {json.dumps(search_dom, ensure_ascii=True)}")
        await box.click()
        await box.press("Control+A")
        await box.press("Delete")
        await box.fill(username)
        # Do NOT press Enter — the results show as an autocomplete dropdown while typing.
        await page.wait_for_timeout(3500)  # diagnostic only — let the dropdown settle

        await page.screenshot(path=str(DIAG_DIR / f"{username}_after_autocomplete.png"))
        print(f"[after-autocomplete] {_host_path(page.url)}")

        # --- autocomplete dropdown (primary results surface) ---
        print("[autocomplete-panel candidates] (count per selector):")
        for sel in selectors.AUTOCOMPLETE_PANEL:
            try:
                n = await page.locator(sel).count()
            except Exception as exc:  # noqa: BLE001
                n = f"<error {type(exc).__name__}>"
            if n:
                print(f"  {sel} -> {n}")

        rendered = selectors.render_autocomplete_suggestions(username)
        print("[autocomplete-suggestion candidates] (count per selector):")
        for sel in rendered:
            try:
                n = await page.locator(sel).count()
            except Exception as exc:  # noqa: BLE001
                n = f"<error {type(exc).__name__}>"
            if n:
                print(f"  {sel} -> {n}")

        suggestions = await find_all_matching(page, rendered)
        print(f"[suggestions found] {len(suggestions)}")
        exact_candidates = []
        for i, sug in enumerate(suggestions[:8]):
            try:
                stext = _clip((await sug.inner_text()).strip(), 300)
            except Exception as exc:  # noqa: BLE001
                stext = f"<error {type(exc).__name__}>"
            print(f"  suggestion[{i}] text: {stext}")
            for u in await _texts_for(sug, selectors.AUTOCOMPLETE_USERNAME):
                print(f"    username? {u}")
            clickable = await find_first_visible(sug, selectors.AUTOCOMPLETE_CLICK_TARGET, timeout_ms=500)
            print(f"    clickable: {clickable is not None}")
            if _clip((await sug.inner_text()).strip()).lower().find(username.lower()) >= 0:
                exact_candidates.append((len(stext), sug))

        # --- click the exact suggestion and inspect what opens ---
        exact = min(exact_candidates, key=lambda item: item[0])[1] if exact_candidates else None
        if exact is not None:
            target = await find_first_visible(
                exact, selectors.AUTOCOMPLETE_CLICK_TARGET, timeout_ms=800
            ) or exact
            try:
                await target.click()
                await page.wait_for_timeout(3000)
                await page.screenshot(path=str(DIAG_DIR / f"{username}_after_suggestion_click.png"))
                print(f"[after-suggestion-click] {_host_path(page.url)}")
                panel = await find_first_visible(page, selectors.DETAIL_PANEL, timeout_ms=4000)
                print(f"[detail-panel] found: {panel is not None}")
                scope = panel if panel is not None else page
                for g in await _texts_for(scope, selectors.GMV_LABELS + selectors.GMV_VALUES):
                    print(f"  detail gmv? {g}")
                for it in await _texts_for(scope, selectors.ITEMS_SOLD_LABELS + selectors.ITEMS_SOLD_VALUES):
                    print(f"  detail items? {it}")
            except Exception as exc:  # noqa: BLE001
                print(f"[suggestion-click] error: {type(exc).__name__}")
        else:
            icon = box.locator(SEARCH_ICON_RELATIVE_SELECTORS[0]).first
            try:
                await icon.click(timeout=1000)
                print("[search-icon] clicked")
            except Exception as exc:  # noqa: BLE001
                print(f"[search-icon] error: {type(exc).__name__}")

        # --- Search results TABLE (GMV/Items sold shown inline per row) ---
        await page.wait_for_timeout(2500)
        await page.screenshot(path=str(DIAG_DIR / f"{username}_after_search_results.png"))
        print(f"[after-search-results] {_host_path(page.url)}")

        print("[search-results section candidates] (count per selector):")
        for sel in selectors.SEARCH_RESULTS_SECTION:
            try:
                n = await page.locator(sel).count()
            except Exception as exc:  # noqa: BLE001
                n = f"<error {type(exc).__name__}>"
            if n:
                print(f"  {sel} -> {n}")

        for g in await _texts_for(page, selectors.SEARCH_RESULT_HEADER_CELLS, limit_each=60):
            print(f"  header? {g}")

        sr_rows = await find_all_matching(page, selectors.SEARCH_RESULT_ROWS)
        print(f"[search-result rows] {len(sr_rows)}")
        for i, row in enumerate(sr_rows[:8]):
            try:
                rtext = _clip((await row.inner_text()).strip(), 500)
            except Exception as exc:  # noqa: BLE001
                rtext = f"<error {type(exc).__name__}>"
            print(f"  sr-row[{i}] text: {rtext}")
            for u in await _texts_for(row, selectors.SEARCH_RESULT_CREATOR_CELL):
                print(f"    username? {u}")
            for g in await _texts_for(row, selectors.SEARCH_RESULT_GMV_CELL):
                print(f"    gmv? {g}")
            for it in await _texts_for(row, selectors.SEARCH_RESULT_ITEMS_SOLD_CELL):
                print(f"    items_sold? {it}")
            for p in await _texts_for(row, selectors.SEARCH_RESULT_PPS):
                print(f"    pps? {p}")
            for a in await _texts_for(row, selectors.SEARCH_RESULT_AUDIENCE):
                print(f"    audience? {a}")

        # --- legacy result table fallback (press Enter) ---
        await box.press("Enter")
        await page.wait_for_timeout(2500)
        await page.screenshot(path=str(DIAG_DIR / f"{username}_after_search.png"))
        print(f"[after-search] {_host_path(page.url)}")
        print("[result-row candidates] (count per selector):")
        for sel in selectors.RESULT_ROWS:
            try:
                n = await page.locator(sel).count()
            except Exception as exc:  # noqa: BLE001
                n = f"<error {type(exc).__name__}>"
            if n:
                print(f"  {sel} -> {n}")

        rows = await find_all_matching(page, selectors.RESULT_ROWS)
        print(f"[rows found] {len(rows)}")
        for i, row in enumerate(rows[:8]):
            with_text = ""
            try:
                with_text = _clip((await row.inner_text()).strip())
            except Exception as exc:  # noqa: BLE001
                with_text = f"<error {type(exc).__name__}>"
            print(f"  row[{i}] text: {with_text}")
            unames = await _texts_for(row, selectors.RESULT_USERNAME)
            for u in unames:
                print(f"    username? {u}")
            gmv = await _texts_for(row, selectors.GMV_LABELS + selectors.GMV_VALUES)
            for g in gmv:
                print(f"    gmv? {g}")

        # --- open first row's detail and inspect ---
        if rows:
            target = await find_first_visible(
                rows[0], selectors.RESULT_ROW_CLICK_TARGET, timeout_ms=1500
            ) or rows[0]
            try:
                await target.click()
                await page.wait_for_timeout(3000)
                await page.screenshot(path=str(DIAG_DIR / f"{username}_after_open.png"))
                print(f"[after-open] {_host_path(page.url)}")
                panel = await find_first_visible(page, selectors.DETAIL_PANEL, timeout_ms=4000)
                print(f"[detail-panel] found: {panel is not None}")
                scope = panel if panel is not None else page
                for g in await _texts_for(scope, selectors.GMV_LABELS + selectors.GMV_VALUES):
                    print(f"  detail gmv? {g}")
                for it in await _texts_for(scope, selectors.ITEMS_SOLD_LABELS + selectors.ITEMS_SOLD_VALUES):
                    print(f"  detail items? {it}")
            except Exception as exc:  # noqa: BLE001
                print(f"[open-detail] error: {type(exc).__name__}")

        try:
            clickable = await page.locator("button, a, [role='button']").count()
            print(f"[clickable elements] {clickable}")
        except Exception:  # noqa: BLE001
            pass

        print(f"\nScreenshots saved under {DIAG_DIR}/")
    finally:
        if context is not None:
            with contextlib.suppress(Exception):
                await context.close()
        await pw.stop()


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: python tools/inspect_creator_result.py <PROFILE_CODE> <username>")
        raise SystemExit(2)
    asyncio.run(inspect(sys.argv[1], sys.argv[2]))


if __name__ == "__main__":
    main()

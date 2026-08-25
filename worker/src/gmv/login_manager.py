"""Manual login manager (spec §9).

Opens a *headed* persistent browser context at the **Seller Center** (``profile.login_url``,
Seller Center) so the user can log in themselves (credentials, CAPTCHA and 2FA are never
automated or stored — spec §9). This is a different host from the Affiliate Center used for GMV
lookups: the affiliate homepage renders a blank/loading screen without a session, so login state
must be confirmed by an authenticated Seller Center element — never by "the URL lacks /login" or
by a still-loading page.

Login-detection selectors are UNVERIFIED against the live DOM (D10); they are ordered fallbacks
and intentionally conservative — when a session cannot be positively confirmed the status is
``login_required``/``expired``, never ``connected``.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from gmv.automation import selectors
from gmv.config import US_PROFILE_CODE, BrowserProfile, ProfileStatus, get_profile

LOGIN_POLL_SECONDS = 300  # how long the login window stays open waiting for success

# TikTok does not always finish a successful login on ``/affiliate/landing``. Depending on
# account state it may send the browser to one of these authenticated Seller Center areas.
# Treating only one exact landing path as success was the reason a real login stayed stuck in
# ``connecting`` for five minutes and kept the browser reservation occupied.
_AUTHENTICATED_SELLER_PREFIXES = (
    "/affiliate",
    "/homepage",
    "/dashboard",
    "/compass",
    "/product",
    "/order",
    "/finance",
    "/analytics",
    "/account/settings",
)


def _host_path(url: str) -> tuple[str | None, str]:
    parsed = urlparse(url or "")
    return parsed.hostname, parsed.path


def is_seller_login_success(url: str) -> bool:
    """True once the browser reaches the Seller Center post-login landing page.

    Matches by host + path prefix only — the query string (``is_new_connect``/``shop_region``)
    may vary and must NOT be required to match exactly. ``about:blank``, ``/account/register``,
    ``accounts.tiktok.com`` and blank/loading screens are all NOT success.
    """
    host, path = _host_path(url)
    return host in {"seller-us.tiktok.com", "seller-uk.tiktok.com"} and path.startswith(
        "/affiliate/landing"
    )


def is_authenticated_seller_url(url: str, expected_host: str | None = None) -> bool:
    """Recognise known post-login Seller Center routes without accepting login pages.

    ``expected_host`` prevents a US profile from being marked connected by an unrelated UK
    tab (and vice versa). Root and account/register pages remain deliberately inconclusive;
    those require an authenticated DOM element instead.
    """
    host, path = _host_path(url)
    if host not in {"seller-us.tiktok.com", "seller-uk.tiktok.com"}:
        return False
    if expected_host is not None and host != expected_host:
        return False
    normalized = (path or "/").lower().rstrip("/") or "/"
    if normalized == "/" or normalized.startswith(
        ("/account/register", "/account/login", "/login", "/register", "/signup")
    ):
        return False
    return any(normalized.startswith(prefix) for prefix in _AUTHENTICATED_SELLER_PREFIXES)


def _log_step(step: str, url: str) -> None:
    """Log only the step, hostname and pathname — never query strings, cookies or tokens (§8)."""
    host, path = _host_path(url)
    print(f"[{step}] host={host} path={path}")


def _status_root() -> Path:
    import os

    root = Path(os.environ.get("GMV_STORAGE_ROOT", "storage/jobs")).parent / "profile_status"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _status_file(code: str) -> Path:
    return _status_root() / f"{code}.json"


def read_status(code: str) -> dict:
    profile = get_profile(code)
    base = {
        "profile_code": profile.profile_code,
        "display_name": profile.display_name,
        "browser_channel": profile.browser_channel,
        "market": profile.market,
        "status": ProfileStatus.DISCONNECTED.value,
        "last_login_at": None,
        "last_verified_at": None,
        "last_used_at": None,
        "last_error": None,
    }
    f = _status_file(code)
    # Preserve the former single-profile Chrome status after upgrading to US_CHROME.
    if not f.exists() and profile.profile_code == US_PROFILE_CODE:
        legacy = _status_file("DEFAULT")
        if legacy.exists():
            f = legacy
    if f.exists():
        base.update(json.loads(f.read_text(encoding="utf-8")))
    # Never let legacy status metadata overwrite the identity of the resolved profile.
    base.update(
        profile_code=profile.profile_code,
        display_name=profile.display_name,
        browser_channel=profile.browser_channel,
        market=profile.market,
    )
    return base


def write_status(code: str, **changes) -> dict:
    data = read_status(code)
    data.update(changes)
    _status_file(code).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


async def _login_form_visible(page) -> bool:
    from gmv.automation.session import find_first_visible

    return await find_first_visible(page, selectors.SELLER_LOGIN_FORM, timeout_ms=1500) is not None


async def detect_seller_login(page) -> ProfileStatus:
    """Positively confirm a Seller Center session, or report that login is required.

    Order (all UNVERIFIED, D10): a login URL or a visible login form => ``LOGIN_REQUIRED``; an
    authenticated Seller Center element => ``CONNECTED``. A blank or still-loading page is
    treated as ``LOGIN_REQUIRED`` — never ``CONNECTED`` (spec §4-§5 of this change).
    """
    from gmv.automation.session import find_first_visible

    if selectors.is_logged_out_url(page.url):
        return ProfileStatus.LOGIN_REQUIRED

    form = await find_first_visible(page, selectors.SELLER_LOGIN_FORM, timeout_ms=1500)
    if form is not None:
        return ProfileStatus.LOGIN_REQUIRED

    logged_in = await find_first_visible(page, selectors.SELLER_LOGGED_IN, timeout_ms=1500)
    if logged_in is not None:
        return ProfileStatus.CONNECTED

    # Neither a login form nor an authenticated element is present (blank/loading/unknown) —
    # cannot positively confirm a session, so require login rather than false-positive.
    return ProfileStatus.LOGIN_REQUIRED


async def _page_is_connected(page, profile: BrowserProfile) -> bool:
    """Check one login-window tab for a positively authenticated Seller session."""
    expected_host, _ = _host_path(profile.login_url)
    try:
        url = page.url
    except Exception:  # closed/replaced tab
        return False

    # Known authenticated routes are the fast and stable path. For TikTok variants that keep
    # the user on another route, fall back to the authenticated Seller Center DOM selectors.
    if is_authenticated_seller_url(url, expected_host):
        return not await _login_form_visible(page)
    try:
        return await detect_seller_login(page) is ProfileStatus.CONNECTED
    except Exception:  # a page can disappear while an OAuth popup closes
        return False


async def open_login(code: str) -> dict:
    """Open a headed Seller Center browser for manual login; return final status (spec §9)."""
    from playwright.async_api import async_playwright

    profile: BrowserProfile = get_profile(code)
    write_status(code, status=ProfileStatus.CONNECTING.value, last_error=None)

    pw = await async_playwright().start()
    context = None
    try:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=profile.storage_root,
            channel=profile.browser_channel,
            headless=False,
            chromium_sandbox=True,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        # Open the Seller Center login entry (NOT the affiliate page).
        await page.goto(profile.login_url, wait_until="domcontentloaded")
        _log_step("login-open", page.url)

        import asyncio

        waited = 0.0
        while waited < LOGIN_POLL_SECONDS:
            # OAuth/QR login may use a popup or replace the original tab, so inspect every live
            # page instead of permanently watching only context.pages[0].
            pages = list(context.pages)
            if not pages:
                # The user closed the login window. Finish immediately instead of holding the
                # API's browser reservation until the full five-minute timeout.
                return write_status(code, status=ProfileStatus.LOGIN_REQUIRED.value)
            for candidate in reversed(pages):
                if await _page_is_connected(candidate, profile):
                    _log_step("login-success", candidate.url)
                    # Login Management confirms only the Seller account. The US security puzzle
                    # belongs to a GMV lookup and is deliberately deferred until the user presses
                    # GMV Lookup Start. Do not open Affiliate/Creator pages from this flow.
                    return write_status(
                        code,
                        status=ProfileStatus.CONNECTED.value,
                        last_login_at=_now(),
                        last_verified_at=_now(),
                        last_error=None,
                    )
            await asyncio.sleep(1)
            waited += 1
        return write_status(code, status=ProfileStatus.LOGIN_REQUIRED.value)
    except Exception as exc:  # noqa: BLE001
        return write_status(code, status=ProfileStatus.ERROR.value, last_error=str(exc)[:300])
    finally:
        if context is not None:
            import contextlib

            with contextlib.suppress(Exception):
                await context.close()
        await pw.stop()


async def verify(code: str) -> dict:
    """Quietly check whether the stored Seller Center session is still logged in (spec §9)."""
    from playwright.async_api import async_playwright

    profile = get_profile(code)
    pw = await async_playwright().start()
    context = None
    try:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=profile.storage_root,
            channel=profile.browser_channel,
            headless=True,
            chromium_sandbox=True,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        # Verify by opening the Seller Center login-success page and seeing whether the session
        # keeps us there (vs. bouncing to register/accounts or showing a login form).
        await page.goto(profile.login_success_url, wait_until="domcontentloaded")
        _log_step("verify", page.url)

        connected = await _page_is_connected(page, profile)
        # Anything else — redirect to /account/register, accounts.tiktok.com, or a blank/loading
        # screen — is treated as needing login, never silently 'connected' (§3).

        return write_status(
            code,
            status=(
                ProfileStatus.CONNECTED.value if connected else ProfileStatus.EXPIRED.value
            ),
            last_verified_at=_now(),
        )
    except Exception as exc:  # noqa: BLE001
        return write_status(code, status=ProfileStatus.ERROR.value, last_error=str(exc)[:300])
    finally:
        if context is not None:
            import contextlib

            with contextlib.suppress(Exception):
                await context.close()
        await pw.stop()


def recover_interrupted_statuses() -> None:
    """Clear a stale ``connecting`` marker left by a stopped/restarted Worker.

    The browser reservation itself lives in memory and disappears on restart, but the status
    JSON persists. Without this recovery the UI can show Connecting forever even though no
    login browser or task exists anymore.
    """
    from gmv.config import list_profiles

    for profile in list_profiles():
        status = read_status(profile.profile_code).get("status")
        if status == ProfileStatus.CONNECTING.value:
            write_status(
                profile.profile_code,
                status=ProfileStatus.LOGIN_REQUIRED.value,
                last_error=None,
            )


def reset(code: str) -> dict:
    """Delete the profile's persistent storage (session reset, spec §9)."""
    profile = get_profile(code)
    path = Path(profile.storage_root)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    return write_status(
        code,
        status=ProfileStatus.DISCONNECTED.value,
        last_login_at=None,
        last_verified_at=None,
        last_error=None,
    )

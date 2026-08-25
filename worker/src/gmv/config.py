"""Independent browser/market profiles for the TikTok GMV workflows."""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class ProfileStatus(str, Enum):
    """Connection lifecycle of a browser profile."""

    DISCONNECTED = "disconnected"
    LOGIN_REQUIRED = "login_required"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RUNNING = "running"
    EXPIRED = "expired"
    ERROR = "error"


# The real, confirmed navigation chain:
#   1. login entry   -> seller-us.tiktok.com/account/register
#   2. login success -> seller-us.tiktok.com/affiliate/landing?...
#   3. affiliate entry (session hand-off) -> affiliate-us.tiktok.com/connection/target-invitation
#   4. find creators  -> affiliate-us.tiktok.com/connection/creator
# Login (Seller Center) and lookup (Affiliate Center) are different hosts and must not be
# conflated: the affiliate pages render blank/loading without a session, which must never be
# treated as "logged in".
US_CHROME_PROFILE_CODE = "US_CHROME"
UK_CHROME_PROFILE_CODE = "UK_CHROME"
US_EDGE_PROFILE_CODE = "US_EDGE"
UK_EDGE_PROFILE_CODE = "UK_EDGE"

# Backward-compatible defaults used by old jobs, spreadsheet market hints, and login state.
US_PROFILE_CODE = US_CHROME_PROFILE_CODE
UK_PROFILE_CODE = UK_EDGE_PROFILE_CODE

# One TikTok login is shared by every lookup lane. Keep the environment-configurable limit
# conservative: raising it above eight would materially increase CAPTCHA/rate-limit risk.
_HARD_MAX_CONCURRENCY = 8


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def get_max_concurrency() -> int:
    """Return the configured lane ceiling, never exceeding the anti-bot hard limit of eight."""
    return min(_HARD_MAX_CONCURRENCY, max(1, _env_int("GMV_MAX_CONCURRENCY", 8)))


def get_default_concurrency() -> int:
    """Return the default number of pages used by a job, clamped to the current ceiling."""
    # The portable UI intentionally opens one TikTok page per GMV job. Independent jobs still
    # run concurrently from their own disposable profiles, so a second site tab creates one
    # additional browser window instead of adding lanes/tabs to the first job's window.
    return min(get_max_concurrency(), max(1, _env_int("GMV_DEFAULT_CONCURRENCY", 1)))


def get_lane_stagger_ms() -> int:
    """Delay between opening/navigating lookup pages to avoid synchronized request bursts."""
    return max(0, _env_int("GMV_LANE_STAGGER_MS", 250))


def default_runtime_root() -> Path:
    """Keep mutable browser/job state out of OneDrive-backed project folders on Windows."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if os.name == "nt" and local_app_data:
        return Path(local_app_data) / "TikTokGMV"
    return Path.cwd() / "storage"


def default_profile_root() -> Path:
    return default_runtime_root() / "profiles"


def default_job_root() -> Path:
    return default_runtime_root() / "jobs"


def default_runtime_profile_root() -> Path:
    """Root for disposable, job-scoped clones of persistent login profiles."""
    return Path(
        os.environ.get(
            "GMV_RUNTIME_PROFILE_ROOT",
            str(default_runtime_root() / "runtime_profiles"),
        )
    )

_US_LOGIN = "https://seller-us.tiktok.com/account/register"
_US_LOGIN_SUCCESS = (
    "https://seller-us.tiktok.com/affiliate/landing?shop_region=US"
)
_US_AFFILIATE_ENTRY = (
    "https://affiliate-us.tiktok.com/affiliate/collaboration/target-invitation"
    "?shop_region=US&route_migration=1&tab=1"
)
_US_FIND_CREATORS = "https://affiliate-us.tiktok.com/connection/creator?shop_region=US"
# Legacy compatibility only — no longer part of the active flow.
_US_AFFILIATE = "https://affiliate-us.tiktok.com/platform/homepage?shop_region=US"

# UK uses a completely separate Edge persistent profile. Seller Center is region-specific,
# while Creator Connection for both markets is served from the shared affiliate.tiktok.com host.
_UK_LOGIN = "https://seller-uk.tiktok.com/"
_UK_LOGIN_SUCCESS = (
    "https://seller-uk.tiktok.com/affiliate/landing?is_new_connect=0&shop_region=GB"
)
_UK_AFFILIATE_ENTRY = (
    "https://affiliate.tiktok.com/connection/target-invitation?shop_region=GB"
)
_UK_FIND_CREATORS = "https://affiliate.tiktok.com/connection/creator?shop_region=GB"
_UK_AFFILIATE = "https://affiliate.tiktok.com/platform/homepage?shop_region=GB"


class BrowserProfile(BaseModel):
    """A single browser environment used for all jobs."""

    profile_code: str
    display_name: str
    browser_channel: str
    market: str
    shop_region: str
    login_url: str  # Seller Center login entry
    login_success_url: str  # where the user lands after a successful login
    affiliate_entry_url: str  # Affiliate Center entry (target-invitation)
    find_creators_url: str  # Find creators search page
    affiliate_url: str  # legacy/compat, not used in the active flow
    storage_key: str
    # Job-scoped clones override only the browser directory. Excluding these runtime-only fields
    # keeps the public profile/API shape unchanged.
    storage_root_override: str | None = Field(default=None, exclude=True, repr=False)
    runtime_job_id: str | None = Field(default=None, exclude=True, repr=False)

    @property
    def storage_root(self) -> str:
        if self.storage_root_override:
            return self.storage_root_override
        base = os.environ.get("GMV_PROFILE_ROOT", str(default_profile_root()))
        return str(Path(base) / self.storage_key)


def get_default_profile() -> BrowserProfile:
    """Return the configured default profile (Chrome/US when unspecified)."""
    return get_profile(os.environ.get("GMV_DEFAULT_PROFILE_CODE", US_PROFILE_CODE))


def _normalize_us_affiliate_host(url: str) -> str:
    """Keep US Affiliate routes on the regional host selected by Seller Center.

    The shared host may redirect back to ``affiliate-us`` anyway. Going through the Seller
    affiliate landing first (handled by the browser session) establishes the required market
    hand-off before these regional routes are opened.
    """
    return url.replace("affiliate.tiktok.com", "affiliate-us.tiktok.com")


def _browser_channel(browser: str) -> str:
    if browser == "CHROME":
        return os.environ.get(
            "GMV_CHROME_BROWSER_CHANNEL",
            os.environ.get("GMV_BROWSER_CHANNEL", "chrome"),
        )
    return os.environ.get(
        "GMV_EDGE_BROWSER_CHANNEL",
        os.environ.get("GMV_UK_BROWSER_CHANNEL", "msedge"),
    )


def _profile(profile_code: str) -> BrowserProfile:
    market, browser = profile_code.split("_", 1)
    browser_name = "Chrome" if browser == "CHROME" else "Edge"
    market_name = "United States" if market == "US" else "United Kingdom"

    if profile_code == US_CHROME_PROFILE_CODE:
        name_env = "GMV_US_PROFILE_NAME"
        storage_env = "GMV_US_PROFILE_STORAGE_KEY"
        # Reuse the former DEFAULT directory so the current Chrome/US login survives upgrade.
        storage_default = "DEFAULT"
    elif profile_code == UK_EDGE_PROFILE_CODE:
        name_env = "GMV_UK_PROFILE_NAME"
        storage_env = "GMV_UK_PROFILE_STORAGE_KEY"
        # Preserve the existing Edge/UK login directory.
        storage_default = UK_EDGE_PROFILE_CODE
    else:
        name_env = f"GMV_{profile_code}_PROFILE_NAME"
        storage_env = f"GMV_{profile_code}_PROFILE_STORAGE_KEY"
        storage_default = profile_code

    if market == "US":
        affiliate_entry_url = _normalize_us_affiliate_host(
            os.environ.get("GMV_AFFILIATE_ENTRY_URL", _US_AFFILIATE_ENTRY)
        )
        find_creators_url = _normalize_us_affiliate_host(
            os.environ.get("GMV_FIND_CREATORS_URL", _US_FIND_CREATORS)
        )
        affiliate_url = _normalize_us_affiliate_host(
            os.environ.get("GMV_AFFILIATE_URL", _US_AFFILIATE)
        )
        login_url = os.environ.get("GMV_LOGIN_URL", _US_LOGIN)
        login_success_url = os.environ.get("GMV_LOGIN_SUCCESS_URL", _US_LOGIN_SUCCESS)
        shop_region = "US"
    else:
        affiliate_entry_url = os.environ.get(
            "GMV_UK_AFFILIATE_ENTRY_URL", _UK_AFFILIATE_ENTRY
        )
        find_creators_url = os.environ.get("GMV_UK_FIND_CREATORS_URL", _UK_FIND_CREATORS)
        affiliate_url = os.environ.get("GMV_UK_AFFILIATE_URL", _UK_AFFILIATE)
        login_url = os.environ.get("GMV_UK_LOGIN_URL", _UK_LOGIN)
        login_success_url = os.environ.get("GMV_UK_LOGIN_SUCCESS_URL", _UK_LOGIN_SUCCESS)
        shop_region = "GB"

    return BrowserProfile(
        profile_code=profile_code,
        display_name=os.environ.get(name_env, f"{browser_name} · {market_name}"),
        browser_channel=_browser_channel(browser),
        market=market,
        shop_region=shop_region,
        login_url=login_url,
        login_success_url=login_success_url,
        affiliate_entry_url=affiliate_entry_url,
        find_creators_url=find_creators_url,
        affiliate_url=affiliate_url,
        storage_key=os.environ.get(storage_env, storage_default),
    )


def _us_profile() -> BrowserProfile:
    """Backward-compatible alias for the original Chrome/US profile."""
    return _profile(US_CHROME_PROFILE_CODE)


def _uk_profile() -> BrowserProfile:
    """Backward-compatible alias for the original Edge/UK profile."""
    return _profile(UK_EDGE_PROFILE_CODE)


def get_profile(profile_code: str) -> BrowserProfile:
    """Resolve a UI/API profile code, keeping ``DEFAULT`` as a legacy US alias."""
    key = str(profile_code or "").strip().upper()
    if key in {US_PROFILE_CODE, "DEFAULT", "US", "CHROME"}:
        return _us_profile()
    if key in {UK_PROFILE_CODE, "UK", "GB", "EDGE"}:
        return _uk_profile()
    if key in {UK_CHROME_PROFILE_CODE, US_EDGE_PROFILE_CODE}:
        return _profile(key)
    raise KeyError(profile_code)


def list_profiles() -> list[BrowserProfile]:
    return [
        _profile(US_CHROME_PROFILE_CODE),
        _profile(UK_CHROME_PROFILE_CODE),
        _profile(US_EDGE_PROFILE_CODE),
        _profile(UK_EDGE_PROFILE_CODE),
    ]


# Column headers that may carry a per-row market (kept for compatibility but ignored).
ACCOUNT_COLUMN_ALIASES = {"market", "region", "account", "shopregion", "국가", "마켓"}


def resolve_profile_code(value: object) -> str | None:
    """Resolve optional spreadsheet hints while keeping legacy market defaults."""
    key = str(value or "").strip().lower().replace(" ", "").replace("_", "")
    if not key:
        return None
    us = {
        "us",
        "usa",
        "unitedstates",
        "미국",
        "chrome",
        "us_chrome",
        "uschrome",
        "default",
    }
    uk = {
        "uk",
        "gb",
        "unitedkingdom",
        "영국",
        "edge",
        "uk_edge",
        "ukedge",
    }
    if key == "ukchrome":
        return UK_CHROME_PROFILE_CODE
    if key == "usedge":
        return US_EDGE_PROFILE_CODE
    if key in us:
        return US_PROFILE_CODE
    if key in uk:
        return UK_PROFILE_CODE
    return None

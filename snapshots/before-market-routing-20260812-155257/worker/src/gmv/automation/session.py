"""Playwright driver for one Affiliate Center account (spec §6, §9, §11).

One persistent browser context per profile is launched ONCE and reused for every creator in
a job (browser/context/page/search screen reused — spec §11). Fixed sleeps are replaced with
event-based waits. Username is verified before any GMV is recorded (spec §16), and a result
signature guards against saving a stale previous result (spec §11).

Playwright is imported lazily so pure modules/tests do not require it.

NOTE: selectors and network shapes are UNVERIFIED against the live DOM (D10) — validate
against a logged-in session before production use.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from urllib.parse import urlparse

from gmv.automation import selectors
from gmv.automation.extractors import extract_from_json
from gmv.config import BrowserProfile, ProfileStatus, get_lane_stagger_ms, get_max_concurrency
from gmv.creator import normalize_username, usernames_match
from gmv.gmv_parser import parse_gmv
from gmv.models import ErrorCode, GmvValueType, JobCancelledError, LookupResult, RowStatus

SEARCH_TIMEOUT_MS = 15_000
NAV_TIMEOUT_MS = 10_000
ENTRY_READY_TIMEOUT_MS = 20_000  # SPA pages settle late; wait on a real DOM element
SEARCH_READY_TIMEOUT_MS = 20_000
# TikTok US commonly presents an interactive puzzle before revealing Creator Search. The user
# must complete that check in the opened browser; automation only waits and resumes afterward.
SECURITY_CHALLENGE_TIMEOUT_MS = 180_000
SECURITY_CHALLENGE_APPEAR_TIMEOUT_MS = 3_000
SECURITY_RECOVERY_APPEAR_TIMEOUT_MS = 1_500
SECURITY_CHALLENGE_CLEAR_STABLE_POLLS = 3

# Keep these selectors local instead of coupling the security gate to the changing Creator DOM.
# The first two target TikTok's known containers; the iframe/class fallbacks cover newer builds.
SECURITY_CHALLENGE_SELECTORS = [
    "#captcha-verify-image",
    ".captcha_verify_container",
    ".secsdk-captcha-drag-icon",
    "iframe[src*='captcha' i]",
    "iframe[src*='verify' i]",
    "[class*='captcha' i]",
    "[id*='captcha' i]",
]


class AffiliateEntryNotReadyError(Exception):
    """The Affiliate Center entry (target-invitation) did not render in time."""


class SearchPageNotFoundError(Exception):
    """The Find-creators page loaded but its search input never appeared."""


class AffiliateSessionExpiredError(Exception):
    """An Affiliate-Center navigation was redirected to a login/account page mid-job."""


class CreatorOpenFailedError(Exception):
    """Clicking a result row / view-details control to open the creator failed."""


class CreatorDetailTimeoutError(Exception):
    """The creator detail panel did not appear within the timeout."""


class SelectorError(Exception):
    """A selector/locator method raised while querying the DOM."""


class AutocompleteClickFailedError(Exception):
    """Clicking the matched autocomplete suggestion failed."""


# Pipeline stages recorded on each row so a failure says WHERE it happened (spec §1).
STAGE_SEARCH_INPUT = "SEARCH_INPUT"
STAGE_WAIT_AUTOCOMPLETE = "WAIT_AUTOCOMPLETE"
STAGE_WAIT_SEARCH_RESULTS = "WAIT_SEARCH_RESULTS"
STAGE_MATCH_USERNAME = "MATCH_USERNAME"
STAGE_OPEN_CREATOR = "OPEN_CREATOR"
STAGE_WAIT_GMV = "WAIT_GMV"
STAGE_EXTRACT_GMV = "EXTRACT_GMV"

# Successful rows still return immediately when their exact account-scoped metrics appear. These
# are only upper bounds for TikTok's slower responses. The earlier sub-second autocomplete/result
# limits produced false CREATOR_NOT_FOUND rows when React committed the exact result late.
DETAIL_TIMEOUT_MS = 1_500
AUTOCOMPLETE_TIMEOUT_MS = 1_200
SEARCH_ICON_RESULT_TIMEOUT_MS = 5_000
SEARCH_RESULTS_TIMEOUT_MS = 3_000
SELECTED_RESULT_TIMEOUT_MS = 3_000

# Live July-2026 DOM: the magnifier is the SVG inside ``core-input-group-suffix``. The separate
# direct SVG sibling of the whole input wrapper is the help icon and must never be used.
SEARCH_ICON_RELATIVE_SELECTORS = [
    (
        "xpath=..//span[contains(concat(' ', normalize-space(@class), ' '), "
        "' core-input-group-suffix ')]//*[name()='svg'][last()]"
    ),
]

# Resolve an exact username node to the account card that owns its avatar. This covers dropdowns
# where the handle itself is a passive span and only its enclosing card receives React's click.
AUTOCOMPLETE_PROFILE_CARD_ANCESTORS = [
    "xpath=ancestor::a[1]",
    "xpath=ancestor::*[@role='option'][1]",
    "xpath=ancestor::*[@role='listitem'][1]",
    "xpath=ancestor::*[@role='button'][1]",
]

# A long workbook must not stop because one React input event, click, or result render was missed.
# The first pass remains the fast path. Only transient failures pay for recovery/retry.
AUTONOMOUS_LOOKUP_ATTEMPTS = max(1, int(os.environ.get("GMV_LOOKUP_ATTEMPTS", "3")))
# TikTok's Creator SPA accumulates detached result/dropdown nodes during very long runs. A
# bounded refresh prevents stale DOM from making row 500+ require a manual click. Set 0 to disable.
LONG_RUN_REFRESH_INTERVAL = max(0, int(os.environ.get("GMV_REFRESH_EVERY", "500")))

_TRANSIENT_LOOKUP_ERRORS = {
    "AUTOCOMPLETE_CLICK_FAILED",
    "BROWSER_ERROR",
    "CREATOR_OPEN_FAILED",
    "GMV_NOT_FOUND",
    "RESULT_ROW_NOT_FOUND",
    "SEARCH_PAGE_NOT_FOUND",
    "SELECTOR_ERROR",
}

_GMV_TYPE_LABELS = {
    GmvValueType.EXACT: "exact",
    GmvValueType.RANGE_MAX: "range_upper",
    GmvValueType.OPEN_ENDED_ESTIMATE: "open_ended",
    GmvValueType.NOT_FOUND: "not_found",
    GmvValueType.ERROR: "error",
}


class _AcInfo:
    """What the autocomplete stage observed (used to choose a precise error)."""

    def __init__(self):
        self.saw_panel = False
        self.saw_suggestions = False
        self.saw_username = False
        # When true, selection must click the resolved card itself. Searching for a nested
        # generic click target first can hit the username span and reproduce the manual-click
        # stall shown in the screenshot.
        self.used_top_profile = False


_HAS_DIGIT = re.compile(r"\d")


def _is_tiktok_error_page(url: str) -> bool:
    """Recognise TikTok's generic SPA error route so it is never treated as connected."""
    parsed = urlparse(url or "")
    return parsed.path.rstrip("/").lower() in {"/errorpage", "/error-page"}


def _is_find_creators_route(url: str) -> bool:
    """Return True on the regional/shared Creator Connection route (ignoring query strings)."""
    return urlparse(url or "").path.rstrip("/").lower() == "/connection/creator"


def _looks_like_money(text: str) -> bool:
    """A money-ish value: has a currency mark or at least one digit (e.g. $1.2K, $0-$5K, 1234)."""
    t = (text or "").strip()
    return bool(t) and ("$" in t or "€" in t or "£" in t or bool(_HAS_DIGIT.search(t)))


def _looks_like_count(text: str) -> bool:
    return bool(_HAS_DIGIT.search(text or ""))


def _safe_exc(exc: Exception) -> str:
    """Bounded, sanitized exception text — strip URLs (which may carry query/tokens), §1/§8."""
    msg = re.sub(r"https?://\S+", "[url]", str(exc))
    return msg[:300]


def _console_safe(text: str, encoding: str | None = None) -> str:
    """Make scraped display text printable on Windows cp949 without affecting extraction.

    Creator names/descriptions often contain emoji. A logging-only UnicodeEncodeError must never
    turn an otherwise valid GMV row into BROWSER_ERROR.
    """
    target_encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return text.encode(target_encoding, errors="backslashreplace").decode(target_encoding)
    except LookupError:
        return text.encode("ascii", errors="backslashreplace").decode("ascii")


def _fail(result: LookupResult, code: ErrorCode, message: str | None = None) -> LookupResult:
    result.status = RowStatus.FAILED
    result.error_code = code
    if message:
        result.error_message = message[:300]
    return result


_HANDLE_RE = re.compile(r"[A-Za-z0-9._]+")


def _username_from_text(text: str | None) -> str | None:
    """Isolate a username handle from row text that may include extra labels/names (§4).

    Handles ``@arlynvibes``, ``arlynvibes`` and ``Creator: arlynvibes``. Prefers an ``@handle``
    token; otherwise the segment after a colon, else the first handle-like token. Returns a bare
    handle (matching is done by :func:`usernames_match`, which normalizes both sides).
    """
    t = (text or "").replace("​", "").strip()
    if not t:
        return None
    at = re.search(r"@([A-Za-z0-9._]+)", t)
    if at:
        return at.group(1)
    if ":" in t:
        t = t.split(":")[-1].strip()
    for token in t.split():
        m = _HANDLE_RE.fullmatch(token.strip().lstrip("@"))
        if m:
            return m.group(0)
    return None


def _parse_count(text: str | None) -> int | None:
    """Expand a count and use the maximum for ranges (``0-100`` -> ``100``)."""
    t = (text or "").strip().lower().replace(",", "")
    values: list[int] = []
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*([km])?", t):
        num = float(match.group(1))
        unit = match.group(2)
        if unit == "k":
            num *= 1_000
        elif unit == "m":
            num *= 1_000_000
        values.append(int(num))
    if not values:
        return None
    return max(values)


# A line that is ONLY a count (optionally with K/M) — used for Items sold (spec §2, §7).
_ITEMS_RE = re.compile(r"^\d[\d,]*(?:\.\d+)?[km]?$", re.IGNORECASE)

# Capture only the money token from a result row. This avoids feeding profile numbers such as
# PPS, followers and audience percentages into the GMV parser when a table renders as one line.
_MONEY_TOKEN_RE = re.compile(
    r"([$€£]\s*\d[\d,]*(?:\.\d+)?\s*[kmb]?"
    r"(?:\s*(?:-|–|—|to)\s*[$€£]?\s*\d[\d,]*(?:\.\d+)?\s*[kmb]?)?\s*\+?)",
    re.IGNORECASE,
)

# The first standalone count after the GMV cell is Items sold in the confirmed table layout.
_COUNT_TOKEN_RE = re.compile(
    r"(?<![\dA-Za-z._])(\d[\d,]*(?:\.\d+)?\s*[km]?"
    r"(?:\s*(?:-|–|—|to)\s*\d[\d,]*(?:\.\d+)?\s*[km]?)?)(?![\dA-Za-z._])",
    re.IGNORECASE,
)


def _items_from_line(line: str | None) -> int | None:
    s = (line or "").strip()
    if not s or "$" in s:
        return None
    if not _ITEMS_RE.fullmatch(s.replace(" ", "")):
        return None
    return _parse_count(s)


def _normalize_text(text: str | None) -> str:
    return (text or "").replace("​", "").lower()


def _text_contains_exact_username(text: str | None, norm: str) -> bool:
    """Match a handle token without accepting prefix accounts (``name`` != ``name2``)."""
    clean = _normalize_text(text)
    wanted = re.escape(normalize_username(norm) or norm.lower())
    return bool(re.search(rf"(?<![a-z0-9._])@?{wanted}(?![a-z0-9._])", clean))


def _text_contains_bare_username(text: str | None, norm: str) -> bool:
    """Match a creator handle without accepting an ``@handle`` hashtag/mention entry."""
    clean = _normalize_text(text)
    wanted = re.escape(normalize_username(norm) or norm.lower())
    return bool(re.search(rf"(?<![@a-z0-9._]){wanted}(?![a-z0-9._])", clean))


def _looks_like_filter_container(text: str | None) -> bool:
    """Reject the page-level Creators / Followers / Performance filter group.

    TikTok renders the search input and all three tabs inside one broad ``div``. When the typed
    handle is also inside that container, a generic ancestor lookup can appear to be an exact
    creator card and a centre click can land on Followers. A real autocomplete account card never
    owns two or more of these page-level tabs.
    """
    clean = _normalize_text(text)
    labels = ("creators", "followers", "performance")
    return sum(bool(re.search(rf"\b{label}\b", clean)) for label in labels) >= 2


async def find_first_visible(
    root,
    selector_candidates: list[str],
    *,
    timeout_ms: int = SEARCH_TIMEOUT_MS,
):
    """Return the first visible locator among heterogeneous selector candidates.

    CSS selectors and Playwright text= selectors must NOT be comma-joined (spec §15). Each
    candidate is tried on its own, in order, so a bad candidate never breaks the group.
    """
    locators = []
    for selector in selector_candidates:
        locator = root.locator(selector).first
        locators.append(locator)
        try:
            if await locator.count() > 0 and await locator.is_visible():
                return locator
        except Exception:  # noqa: BLE001
            continue

    if timeout_ms <= 0:
        return None

    # Wait for all fallbacks concurrently. The old sequential loop applied at least 250 ms to
    # *every* missing selector, so a nominal 350 ms lookup could actually take well over a
    # second. The timeout is now a true total cap while preserving ordered fast-path priority.
    async def wait_one(locator):
        try:
            await locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except Exception:  # noqa: BLE001
            return None

    tasks = [asyncio.create_task(wait_one(locator)) for locator in locators]
    try:
        for completed in asyncio.as_completed(tasks):
            locator = await completed
            if locator is not None:
                # Multiple fallbacks can become visible in the same render. Re-scan in declared
                # order so concurrency changes latency, not selector priority.
                for preferred in locators:
                    try:
                        if await preferred.count() > 0 and await preferred.is_visible():
                            return preferred
                    except Exception:  # noqa: BLE001
                        # Minimal Playwright-compatible wrappers may implement ``wait_for`` but
                        # not ``is_visible``. The completed wait is still valid evidence.
                        if preferred is locator:
                            return locator
                        continue
        return None
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def find_all_matching(root, selector_candidates: list[str]) -> list:
    """Return every element matched by any candidate selector, checked individually (spec §15)."""
    found: list = []
    for selector in selector_candidates:
        with contextlib.suppress(Exception):
            found.extend(await root.locator(selector).all())
    return found


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class _Lane:
    """All mutable browser state owned by one lookup page/tab."""

    page: object | None = None
    captured_json: list = field(default_factory=list)
    search_box: object | None = None
    last_opened_profile: str | None = None
    active_row_username: str | None = None
    active_row_text: str | None = None
    creators_filter_ready: bool = False
    lookup_security_passed: bool = False
    completed_lookups: int = 0
    consecutive_transient_failures: int = 0
    security_challenge_active: bool = False


class TikTokAffiliateSession:
    """Drives one market's Affiliate Center. Create, ``start``, ``search_creator`` N times, ``close``."""

    def __init__(self, profile: BrowserProfile, headless: bool = False):
        self.profile = profile
        self.headless = headless
        self._pw = None
        self._context = None
        self._default_lane = _Lane()
        self._lane_var: ContextVar[_Lane | None] = ContextVar(
            f"gmv_lane_{id(self)}", default=None
        )
        self._lanes: list[_Lane] = []
        self._lane_queue: asyncio.Queue[_Lane] | None = None
        # The runner fills this before an automatic watchdog restart. It prevents a browser the
        # user deliberately minimized from being relaunched in front of every other application.
        # The initial login/start remains visible because this is None on the first session.
        self.startup_window_state: str | None = None
        # Read by the outer job watchdog. A genuinely visible TikTok puzzle is the only state
        # allowed to suspend the normal no-progress timeout; a missing search input alone is not
        # evidence of verification and must never create a silent three-minute wait.
        self.security_challenge_active = False
        # Set by the runner before a job; a set() event means the user asked to stop (§6).
        self.cancel_event = None

    def _active_lane(self) -> _Lane:
        return self._lane_var.get() or self._default_lane

    # Backward-compatible attributes used by the original single-page tests. During a pooled
    # lookup these resolve through a ContextVar, so concurrent tasks cannot clobber one another.
    @property
    def _page(self):
        return self._active_lane().page

    @_page.setter
    def _page(self, value) -> None:
        self._active_lane().page = value

    @property
    def _captured_json(self) -> list:
        return self._active_lane().captured_json

    @_captured_json.setter
    def _captured_json(self, value: list) -> None:
        self._active_lane().captured_json = value

    @property
    def _search_box(self):
        return self._active_lane().search_box

    @_search_box.setter
    def _search_box(self, value) -> None:
        self._active_lane().search_box = value

    @property
    def security_challenge_active(self) -> bool:
        current = self._lane_var.get()
        if current is not None:
            return current.security_challenge_active
        lanes = self._lanes or [self._default_lane]
        return any(lane.security_challenge_active for lane in lanes)

    @security_challenge_active.setter
    def security_challenge_active(self, value: bool) -> None:
        self._active_lane().security_challenge_active = value

    @staticmethod
    def _lane_value_property(name: str):
        def get(self):
            return getattr(self._active_lane(), name)

        def set_(self, value) -> None:
            setattr(self._active_lane(), name, value)

        return property(get, set_)

    _last_opened_profile = _lane_value_property("last_opened_profile")
    _active_row_username = _lane_value_property("active_row_username")
    _active_row_text = _lane_value_property("active_row_text")
    _creators_filter_ready = _lane_value_property("creators_filter_ready")
    _lookup_security_passed = _lane_value_property("lookup_security_passed")
    _completed_lookups = _lane_value_property("completed_lookups")
    _consecutive_transient_failures = _lane_value_property("consecutive_transient_failures")

    def _check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise JobCancelledError

    async def start(self, concurrency: int = 1) -> ProfileStatus:
        from playwright.async_api import async_playwright

        self._check_cancel()  # before launching the browser (§6)
        concurrency = min(get_max_concurrency(), max(1, int(concurrency)))
        stagger_seconds = get_lane_stagger_ms() / 1000
        print(
            f"[browser-session] action=start job_id={self.profile.runtime_job_id or '-'} "
            f"profile={self.profile.profile_code} browser={self.profile.browser_channel} "
            f"user_data_dir={self.profile.storage_root} lanes={concurrency}"
        )
        self._pw = await async_playwright().start()
        # Dedicated persistent context per profile -> isolated cookies/storage (spec §6).
        launch_args = ["--start-minimized"] if self.startup_window_state == "minimized" else []
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=self.profile.storage_root,
            channel=self.profile.browser_channel,
            headless=self.headless,
            # Playwright otherwise disables Chromium's sandbox by default. Explicitly enabling
            # it is the supported way to avoid Chrome's unsupported --no-sandbox warning.
            chromium_sandbox=True,
            args=launch_args,
        )
        first_page = (
            self._context.pages[0] if self._context.pages else await self._context.new_page()
        )
        self._default_lane.page = first_page
        self._lanes = [self._default_lane]
        for _ in range(1, concurrency):
            self._check_cancel()
            if stagger_seconds:
                await asyncio.sleep(stagger_seconds)
            self._lanes.append(_Lane(page=await self._context.new_page()))

        for lane in self._lanes:
            lane.page.on(
                "response",
                partial(self._on_response, lane=lane),
            )
        await self.restore_window_state(self.startup_window_state)
        # The US security challenge is intentionally triggered here, only after the user presses
        # GMV Lookup Start. Creator Search is not opened until the manual challenge is complete.
        await self._goto_find_creators_with_recovery()
        status = await self.check_login()
        if status is not ProfileStatus.CONNECTED:
            return status

        search = await self._wait_for_creator_search_ready()
        if search is None:
            return ProfileStatus.ERROR

        # The first page performs the login/security hand-off. Other pages share that context and
        # cookie jar, then navigate with a small offset so they do not burst identical requests.
        for lane in self._lanes[1:]:
            self._check_cancel()
            if stagger_seconds:
                await asyncio.sleep(stagger_seconds)
            lane.lookup_security_passed = self._default_lane.lookup_security_passed
            token = self._lane_var.set(lane)
            try:
                await self._goto_find_creators_with_recovery()
                status = await self.check_login()
                if status is not ProfileStatus.CONNECTED:
                    return status
                if await self._wait_for_creator_search_ready() is None:
                    return ProfileStatus.ERROR
            finally:
                self._lane_var.reset(token)

        self._lane_queue = asyncio.Queue(maxsize=len(self._lanes))
        for lane in self._lanes:
            self._lane_queue.put_nowait(lane)
        return ProfileStatus.CONNECTED

    async def capture_window_state(self) -> str | None:
        """Return the Chromium window state used to preserve minimize across auto-restarts."""
        if self.headless or self._context is None or self._page is None:
            return None
        cdp = None
        try:
            cdp = await self._context.new_cdp_session(self._page)
            window = await cdp.send("Browser.getWindowForTarget")
            state = str((window.get("bounds") or {}).get("windowState") or "").lower()
            return state if state in {"minimized", "maximized", "fullscreen", "normal"} else None
        except Exception:  # noqa: BLE001 - window management must never stop a GMV job
            return None
        finally:
            if cdp is not None:
                with contextlib.suppress(Exception):
                    await cdp.detach()

    async def restore_window_state(self, state: str | None) -> None:
        """Apply a captured state to a newly created automatic-recovery browser window."""
        if (
            self.headless
            or state not in {"minimized", "maximized", "fullscreen", "normal"}
            or self._context is None
            or self._page is None
        ):
            return
        cdp = None
        try:
            cdp = await self._context.new_cdp_session(self._page)
            window = await cdp.send("Browser.getWindowForTarget")
            await cdp.send(
                "Browser.setWindowBounds",
                {"windowId": window["windowId"], "bounds": {"windowState": state}},
            )
            print(f"[browser-window] restored-state={state}")
        except Exception:  # noqa: BLE001 - focus preservation is best effort, lookup must continue
            return
        finally:
            if cdp is not None:
                with contextlib.suppress(Exception):
                    await cdp.detach()

    async def _wait_for_lookup_security_gate(self) -> ProfileStatus:
        """Open the US lookup gate and wait for the user to finish TikTok's puzzle.

        This method never opens Creator Search. While the puzzle is visible it performs no
        navigation at all. A stable no-puzzle state is accepted for sessions TikTok has already
        verified, so repeat jobs do not wait unnecessarily.
        """
        if self.profile.market != "US" or self._lookup_security_passed:
            return ProfileStatus.CONNECTED

        self._check_cancel()
        await self._page.goto(self.profile.login_success_url, wait_until="domcontentloaded")
        self._log_step("lookup-security-entry")
        return await self._observe_lookup_security_gate()

    async def _observe_lookup_security_gate(
        self, appear_timeout_ms: int | None = None
    ) -> ProfileStatus:
        """Observe the current page without navigating until a security widget is cleared.

        TikTok occasionally replaces the verification page with ``/errorpage`` immediately after
        a valid puzzle submission. Once a challenge has actually been seen, that transition is a
        completion signal, not a reason to interrupt the job. The caller performs one bounded
        Seller-entry recovery before opening Creator Search.
        """
        if appear_timeout_ms is None:
            appear_timeout_ms = SECURITY_CHALLENGE_APPEAR_TIMEOUT_MS
        loop = asyncio.get_running_loop()
        appear_deadline = loop.time() + (appear_timeout_ms / 1000)
        challenge_deadline = None
        challenge_seen = False
        clear_polls = 0

        while True:
            self._check_cancel()
            challenge = await self._visible_security_challenge()
            if challenge is not None:
                self.security_challenge_active = True
                clear_polls = 0
                if not challenge_seen:
                    challenge_seen = True
                    challenge_deadline = loop.time() + (SECURITY_CHALLENGE_TIMEOUT_MS / 1000)
                    print(
                        "[lookup-security-check] TikTok puzzle detected; complete it in the "
                        "browser. Creator Search will stay closed until verification finishes"
                    )
            elif challenge_seen:
                clear_polls += 1
                if clear_polls >= SECURITY_CHALLENGE_CLEAR_STABLE_POLLS:
                    self.security_challenge_active = False
                    self._lookup_security_passed = True
                    print(
                        "[lookup-security-check] verification completed; preparing Affiliate session"
                    )
                    return ProfileStatus.CONNECTED
            else:
                # Never navigate while a visible puzzle is being solved. Only evaluate redirects
                # before the puzzle appears, or after it has been stably absent.
                if selectors.is_affiliate_login_redirect(self._page.url):
                    return ProfileStatus.LOGIN_REQUIRED
                if _is_tiktok_error_page(self._page.url):
                    return ProfileStatus.ERROR

            if not challenge_seen and loop.time() >= appear_deadline:
                self.security_challenge_active = False
                self._lookup_security_passed = True
                print(
                    "[lookup-security-check] no verification required; opening Creator Search"
                )
                return ProfileStatus.CONNECTED

            if challenge_deadline is not None and loop.time() >= challenge_deadline:
                self.security_challenge_active = False
                print("[lookup-security-check] timed out before verification was completed")
                return ProfileStatus.ERROR
            await asyncio.sleep(0.2)

    async def _goto_find_creators_with_recovery(self) -> None:
        """Open Creator Connection and recover once from TikTok's generic error page.

        US: open the Seller security entry, conditionally wait for manual verification, and only
        then open Creator Search. If TikTok itself shows ``/errorpage`` after a successful puzzle,
        revisit the Seller hand-off once and continue with the now-verified persistent session.
        The obsolete target-invitation route is deliberately not used.

        UK keeps the existing Seller -> Affiliate entry hand-off.
        """
        if self.profile.market == "US":
            gate_status = await self._wait_for_lookup_security_gate()
            if gate_status is not ProfileStatus.CONNECTED:
                return

            # A verified challenge can legitimately finish on TikTok's generic error route.
            # Reopen Seller's Affiliate landing once so it can publish the verified shop session.
            # This happens only after the puzzle has disappeared; no navigation occurs while the
            # user is solving it.
            if _is_tiktok_error_page(self._page.url):
                self._check_cancel()
                await self._page.goto(self.profile.login_success_url, wait_until="domcontentloaded")
                self._log_step("post-verification-error-recovery")
                recovery_status = await self._observe_lookup_security_gate(
                    appear_timeout_ms=SECURITY_RECOVERY_APPEAR_TIMEOUT_MS
                )
                if recovery_status is ProfileStatus.LOGIN_REQUIRED:
                    return
                # Some accounts keep Seller landing on /errorpage although the verification
                # cookie is already committed. Continue once to Creator instead of looping Retry.
                if recovery_status is ProfileStatus.ERROR and not self._lookup_security_passed:
                    return

            for attempt in range(2):
                self._check_cancel()
                await self._page.goto(self.profile.find_creators_url, wait_until="domcontentloaded")
                self._log_step("find-creators" if attempt == 0 else "find-creators-recovered")
                if not _is_tiktok_error_page(self._page.url):
                    return
                if attempt == 0:
                    # One bounded Seller hand-off refresh repairs stale regional Affiliate state.
                    # If TikTok unexpectedly asks for verification again, wait on this exact page.
                    await self._page.goto(
                        self.profile.login_success_url, wait_until="domcontentloaded"
                    )
                    self._log_step("creator-error-seller-recovery")
                    recovery_status = await self._observe_lookup_security_gate(
                        appear_timeout_ms=SECURITY_RECOVERY_APPEAR_TIMEOUT_MS
                    )
                    if recovery_status is ProfileStatus.LOGIN_REQUIRED:
                        return
            return

        for attempt in range(2):
            self._check_cancel()
            await self._page.goto(self.profile.login_success_url, wait_until="domcontentloaded")
            self._log_step("seller-affiliate-handoff")
            if selectors.is_affiliate_login_redirect(self._page.url):
                return

            # Seller Center may complete the hand-off asynchronously. If it redirects this tab,
            # let that finish; otherwise explicitly open the Affiliate entry route.
            with contextlib.suppress(Exception):
                await self._page.wait_for_url("**/connection/**", timeout=5_000)
            if "connection/" not in self._page.url:
                await self._page.goto(
                    self.profile.affiliate_entry_url, wait_until="domcontentloaded"
                )
                self._log_step("affiliate-session-entry")

            if selectors.is_affiliate_login_redirect(self._page.url):
                return
            await self._page.goto(self.profile.find_creators_url, wait_until="domcontentloaded")
            self._log_step("find-creators" if attempt == 0 else "find-creators-recovered")
            if not _is_tiktok_error_page(self._page.url):
                return

    def _log_step(self, step: str) -> None:
        """Log only step + hostname + pathname — never query strings, cookies or tokens (§8)."""
        from urllib.parse import urlparse

        parsed = urlparse(self._page.url if self._page else "")
        print(f"[{step}] host={parsed.hostname} path={parsed.path}")

    async def _on_response(self, response, lane: _Lane | None = None) -> None:
        lane = lane or self._active_lane()
        ctype = response.headers.get("content-type", "")
        if "application/json" not in ctype:
            return
        with contextlib.suppress(Exception):
            lane.captured_json.append(await response.json())
        # Keep only the most recent handful to bound memory.
        if len(lane.captured_json) > 20:
            lane.captured_json = lane.captured_json[-20:]

    async def check_login(self) -> ProfileStatus:
        if self._page is None:
            return ProfileStatus.DISCONNECTED
        if _is_tiktok_error_page(self._page.url):
            return ProfileStatus.ERROR
        # On the Affiliate Center, a redirect to any account/login page means no session (§7).
        if selectors.is_affiliate_login_redirect(self._page.url):
            return ProfileStatus.LOGIN_REQUIRED
        return ProfileStatus.CONNECTED

    async def search_creator(self, requested: str) -> LookupResult:
        """Acquire one page lane, keeping direct pre-start test usage backward compatible."""
        if self._lane_queue is None:
            return await self._search_creator_in_lane(requested)

        lane = await self._lane_queue.get()
        token = self._lane_var.set(lane)
        try:
            return await self._search_creator_in_lane(requested)
        finally:
            self._lane_var.reset(token)
            self._lane_queue.put_nowait(lane)

    async def _search_creator_in_lane(self, requested: str) -> LookupResult:
        """Lookup one creator with fully automatic, bounded self-recovery.

        A normal creator uses one pass. A missed autocomplete/click/result update is retried
        without operator input; the last attempt refreshes Creator Search to discard stale SPA
        state. A creator that is absent from the search results fails immediately so a large job
        can continue with the next row; GMV-only failures receive one verification retry. Login
        expiry, parse errors and valid GMV results are never retried.
        """
        self._check_cancel()
        self._completed_lookups += 1

        if (
            LONG_RUN_REFRESH_INTERVAL
            and self._completed_lookups > 1
            and (self._completed_lookups - 1) % LONG_RUN_REFRESH_INTERVAL == 0
        ):
            try:
                await self._refresh_creator_workspace(reason="long-run-maintenance")
            except JobCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one maintenance refresh must not stop 10k rows
                self._search_box = None
                print(f"[creator-search] maintenance-recovery-skipped error={_safe_exc(exc)}")

        last_result = None
        for attempt in range(AUTONOMOUS_LOOKUP_ATTEMPTS):
            self._check_cancel()
            if attempt:
                try:
                    await self._recover_lookup_state(hard=attempt >= 2)
                except JobCancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - continue into the bounded lookup attempt
                    self._search_box = None
                    print(f"[creator-search] auto-recovery-error={_safe_exc(exc)}")
                print(
                    f"[creator-search] auto-retry={attempt + 1}/{AUTONOMOUS_LOOKUP_ATTEMPTS} "
                    f"creator={normalize_username(requested) or requested}"
                )

            result = await self._search_creator_once(requested)
            last_result = result
            if result.status in (RowStatus.SUCCESS, RowStatus.RANGE):
                self._consecutive_transient_failures = 0
                return result

            code = getattr(result.error_code, "value", result.error_code)
            code_name = str(code or "").upper()
            if code_name not in _TRANSIENT_LOOKUP_ERRORS:
                self._consecutive_transient_failures = 0
                return result
            # A missing creator is not transient and has already returned above. A missing GMV
            # receives one verification pass because the row may have rendered before its metric.
            if code_name == "GMV_NOT_FOUND" and attempt >= 1:
                return result
            self._consecutive_transient_failures += 1

        return last_result

    async def _recover_lookup_state(self, *, hard: bool) -> None:
        """Restore a usable search screen after a transient row failure, without manual clicks."""
        self._check_cancel()
        self._captured_json.clear()

        if self._search_box is not None:
            with contextlib.suppress(Exception):
                await self._search_box.press("Escape")
            with contextlib.suppress(Exception):
                await self._search_box.fill("")

        # Re-resolve the React input on every retry. The locator often points to a detached node
        # after TikTok remounts its search header.
        self._search_box = None
        if hard or _is_tiktok_error_page(self._page.url):
            await self._refresh_creator_workspace(reason="transient-failure")
            return

        # Soft recovery is intentionally navigation-free and therefore faster. If the input was
        # actually removed, ensure_find_creators_page performs the bounded page recovery.
        with contextlib.suppress(Exception):
            await self.ensure_find_creators_page()

    async def _refresh_creator_workspace(self, *, reason: str) -> None:
        """Refresh the Creator SPA and wait until the real, uncovered search input is ready."""
        self._check_cancel()
        self._search_box = None
        self._creators_filter_ready = False
        self._captured_json.clear()
        print(f"[creator-search] workspace-refresh reason={reason}")

        refreshed = False
        if _is_find_creators_route(self._page.url) and not _is_tiktok_error_page(self._page.url):
            with contextlib.suppress(Exception):
                await self._page.reload(wait_until="domcontentloaded")
                refreshed = True

        if refreshed:
            search = await self._wait_for_creator_search_ready()
            if search is not None:
                self._search_box = search
                return

        # Reload may be unavailable after a renderer crash or the page may have landed on TikTok's
        # error route. Use the existing bounded Seller/Creator recovery; it also pauses if a real
        # platform security challenge appears.
        await self._goto_find_creators_with_recovery()
        search = await self._wait_for_creator_search_ready()
        if search is not None:
            self._search_box = search

    async def _search_creator_once(self, requested: str) -> LookupResult:
        norm = normalize_username(requested)
        result = LookupResult(
            normalized_username=norm or requested,
            account_label=self.profile.profile_code,
            queried_at=_now_iso(),
        )
        if norm is None:
            result.status = RowStatus.FAILED
            result.error_code = ErrorCode.PARSE_ERROR
            result.error_message = "empty username"
            return result

        try:
            self._check_cancel()  # before starting this creator (§6)
            if selectors.is_affiliate_login_redirect(self._page.url):
                result.status = RowStatus.FAILED
                result.error_code = ErrorCode.SESSION_EXPIRED
                return result

            result.current_stage = STAGE_SEARCH_INPUT
            self._captured_json.clear()
            try:
                search = await self.ensure_find_creators_page()
            except AffiliateSessionExpiredError:
                return _fail(result, ErrorCode.SESSION_EXPIRED, "creator page redirected to login")
            except SearchPageNotFoundError:
                return _fail(
                    result, ErrorCode.SEARCH_PAGE_NOT_FOUND, "search input not found on creator page"
                )

            # A previous buggy run may have left the persistent SPA on Followers. Restore the
            # page-level Creators filter once, before typing opens any autocomplete dropdown.
            if await self._ensure_creators_filter_active():
                self._search_box = None
                refreshed_search = await self._has_search_input()
                if refreshed_search is not None:
                    search = refreshed_search

            # Reset the previous dropdown/result state before every creator. TikTok occasionally
            # keeps a stale autocomplete panel after two successful lookups; simply replacing the
            # text then does not produce a fresh selectable result.
            try:
                await self._prepare_search_input(search, norm)
            except Exception:  # noqa: BLE001 -- cached SPA input may have been replaced
                # TikTok occasionally remounts the search input after a couple of selections.
                # Re-resolve it once rather than failing the remaining workbook rows.
                self._search_box = None
                search = await self.ensure_find_creators_page()
                await self._prepare_search_input(search, norm)
            self._search_box = search
            # Do not submit the page-level search form; only an exact autocomplete card is safe.
            return await self._identify_and_extract(norm, result)
        except JobCancelledError:
            raise  # cooperative cancel must propagate, never become a row error
        except CreatorOpenFailedError as exc:
            return _fail(result, ErrorCode.CREATOR_OPEN_FAILED, _safe_exc(exc))
        except CreatorDetailTimeoutError as exc:
            return _fail(result, ErrorCode.CREATOR_DETAIL_TIMEOUT, _safe_exc(exc))
        except SelectorError as exc:
            return _fail(result, ErrorCode.SELECTOR_ERROR, _safe_exc(exc))
        except Exception as exc:  # noqa: BLE001 — only truly unexpected Playwright errors land here
            # Preserve the exception TYPE + a bounded, safe message (no cookies/tokens/URLs, §1).
            return _fail(result, ErrorCode.BROWSER_ERROR, f"{type(exc).__name__}: {_safe_exc(exc)}")

    async def _ensure_creators_filter_active(self) -> bool:
        """Select the exact page-level Creators tab once; never target Followers or siblings."""
        if self._creators_filter_ready:
            return False

        try:
            candidates = await self._page.get_by_text("Creators", exact=True).all()
        except Exception:  # noqa: BLE001 - builds without get_by_text retry on the next lookup
            return False

        for candidate in candidates:
            try:
                if not await candidate.is_visible():
                    continue
                if (await candidate.inner_text()).strip().lower() != "creators":
                    continue
            except Exception:  # noqa: BLE001
                continue

            selected = None
            with contextlib.suppress(Exception):
                selected = (await candidate.get_attribute("aria-selected") or "").lower()
            if selected == "true":
                self._creators_filter_ready = True
                print("[creator-search] filter=creators state=already-active")
                return False

            try:
                try:
                    await candidate.click(timeout=300)
                except TypeError:
                    await candidate.click()
            except Exception:  # noqa: BLE001 - try another exact Creators label if present
                continue

            self._creators_filter_ready = True
            print("[creator-search] filter=creators state=restored")
            await asyncio.sleep(0.05)
            return True
        return False

    async def _prepare_search_input(self, search, norm: str) -> None:
        """Clear the previous lookup UI, then emit a fresh input event for ``norm``.

        Escape closes an open suggestion/detail overlay in TikTok builds that keep it mounted.
        ``fill('')`` is intentional even though the next fill replaces the value: it makes React
        observe a real value transition when several creators are searched in one reused page.
        """
        with contextlib.suppress(Exception):
            await search.press("Escape")
        self._last_opened_profile = None
        self._active_row_username = None
        self._active_row_text = None
        await search.fill("")
        await search.fill(norm)
        # ``fill`` normally emits input/change, but TikTok has shipped builds that listen on a
        # wrapper and miss a later programmatic fill after several searches. Re-dispatching the
        # native events is harmless and avoids leaving only visible text in the field.
        with contextlib.suppress(Exception):
            await search.dispatch_event("input")
        with contextlib.suppress(Exception):
            await search.dispatch_event("change")

    async def _retrigger_autocomplete(self, norm: str) -> None:
        """Retry only a delayed/missed autocomplete without reloading Creator Search.

        ``fill`` is fastest and remains the normal path. On a missed dropdown, keyboard-style
        input is used once because TikTok sometimes listens for key events after repeated
        searches. This is the programmatic equivalent of the manual typing/click that previously
        unblocked the third creator.
        """
        if self._search_box is None:
            return
        search = self._search_box
        with contextlib.suppress(Exception):
            await search.press("Escape")
        await search.fill("")
        press_sequentially = getattr(search, "press_sequentially", None)
        if press_sequentially is None:
            await search.fill(norm)
        else:
            try:
                await press_sequentially(norm, delay=8)
            except Exception:  # noqa: BLE001 -- restore the exact value if typing was interrupted
                await search.fill(norm)
        with contextlib.suppress(Exception):
            await search.dispatch_event("input")

    async def _creator_activation_state(self, norm: str) -> tuple[bool, bool]:
        """Return ``(exact_creator_visible, row_has_money)`` for the current query."""
        # The page already fetched this response as part of the normal search. An exact username
        # match is both faster and safer than repeatedly walking a large virtualized DOM.
        if self._network_metrics(norm) is not None:
            return True, True

        direct = await self._row_text_from_result_rows(norm)
        if direct is not None:
            return True, True

        # The open autocomplete itself contains the exact username, but that is not proof that
        # the account click was accepted. Treat it as inactive until the Creators dropdown closes;
        # otherwise the loop stops on precisely the screenshot state and waits for a human click.
        if await self._top_creator_profile_suggestion(norm) is not None:
            return False, False

        # A TikTok result can be a div-based virtual row that is absent from the stable table
        # selectors. Walking only ancestors of the exact username keeps the check account-scoped.
        for element in await self._find_creator_elements(norm):
            row_text, any_text = await self._row_text_from_ancestors(element, norm)
            if row_text is not None:
                return True, True
            if any_text is not None:
                return True, False
        return False, False

    async def _wait_for_creator_activation(
        self, norm: str, timeout_ms: int = SELECTED_RESULT_TIMEOUT_MS
    ) -> tuple[bool, bool]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (timeout_ms / 1000)
        while loop.time() < deadline:
            self._check_cancel()
            state = await self._creator_activation_state(norm)
            if state[1]:
                return state
            # Preserve an exact-but-not-yet-populated row while allowing the GMV cells a moment
            # to render. The caller can then click the profile automatically if they stay empty.
            remaining = max(0.0, deadline - loop.time())
            if state[0] and remaining <= min(0.2, timeout_ms / 2000):
                return state
            await asyncio.sleep(0.04)
        return await self._creator_activation_state(norm)

    async def _submit_search_with_icon(self, norm: str) -> tuple[bool, bool]:
        """Click only the magnifier adjacent to the active input, with a geometry guard."""
        if self._search_box is None:
            return False, False
        previous_signature = await self._result_table_signature()

        input_box = None
        with contextlib.suppress(Exception):
            input_box = await self._search_box.bounding_box()
        if not input_box:
            return False, False

        clicked = False
        for selector in SEARCH_ICON_RELATIVE_SELECTORS:
            try:
                candidate = self._search_box.locator(selector).first
                if await candidate.count() == 0 or not await candidate.is_visible():
                    continue
                icon_box = await candidate.bounding_box()
                if not icon_box:
                    continue

                input_right = input_box["x"] + input_box["width"]
                icon_center_x = icon_box["x"] + icon_box["width"] / 2
                icon_center_y = icon_box["y"] + icon_box["height"] / 2
                # The magnifier must sit immediately beside the input and overlap it vertically.
                # Followers is a full row below and therefore can never pass this check.
                if not (
                    input_right - 50 <= icon_center_x <= input_right + 70
                    and input_box["y"] <= icon_center_y <= input_box["y"] + input_box["height"]
                ):
                    continue
                try:
                    await candidate.click(timeout=200)
                except TypeError:
                    await candidate.click()
                clicked = True
                break
            except Exception:  # noqa: BLE001 - never broaden into a page-level click fallback
                continue

        if not clicked:
            print(f"[creator-search] submit-skipped={norm} reason=no-safe-search-icon")
            return False, False

        print(f"[creator-search] submitted={norm} method=adjacent-search-icon")
        return await self._wait_for_submitted_result(norm, previous_signature)

    async def _result_table_signature(self) -> str:
        """Bounded signature used to detect that TikTok completed a search with no exact match."""
        # Selector fallbacks describe the same rows. Querying all of them first can materialize
        # hundreds of duplicate locators in TikTok's virtual table. Stop at the first selector
        # that yields readable rows.
        for selector in selectors.SEARCH_RESULT_ROWS:
            parts = []
            with contextlib.suppress(Exception):
                rows = await self._page.locator(selector).all()
                for row in rows:
                    if len(parts) >= 6:
                        break
                    with contextlib.suppress(Exception):
                        if await row.is_visible():
                            text = " ".join((await row.inner_text()).split())[:240]
                            if text:
                                parts.append(text)
            if parts:
                return "\n".join(parts)
        return ""

    async def _wait_for_submitted_result(
        self, norm: str, previous_signature: str
    ) -> tuple[bool, bool]:
        """Wait for the exact row for the full bounded window.

        TikTok replaces the old table before React commits the new row text. Treating the first
        signature change as a completed empty search caused valid creators to fail even though
        their GMV row appeared a moment later. ``previous_signature`` is retained for diagnostics
        and API compatibility, but a changed table is never proof that the exact creator is absent.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (SEARCH_ICON_RESULT_TIMEOUT_MS / 1000)
        saw_table_change = False
        while loop.time() < deadline:
            self._check_cancel()
            state = await self._creator_activation_state(norm)
            if state[0]:
                return state

            if not saw_table_change:
                current_signature = await self._result_table_signature()
                saw_table_change = bool(
                    current_signature and current_signature != previous_signature
                )
            await asyncio.sleep(0.05)
        return await self._creator_activation_state(norm)

    async def _click_matching_result_profile(self, norm: str) -> bool:
        """Click the exact result row/profile when the search ran but metrics stayed unopened."""
        candidates = selectors.SEARCH_RESULT_ROWS + selectors.RESULT_ROW
        for selector in candidates:
            with contextlib.suppress(Exception):
                rows = await self._page.locator(selector).all()
                for row in rows:
                    try:
                        if not await row.is_visible():
                            continue
                        text = await row.inner_text()
                        if (
                            not usernames_match(norm, _username_from_text(text) or "")
                            and not _text_contains_exact_username(text, norm)
                        ):
                            continue
                        await self._click_open(row, ErrorCode.CREATOR_OPEN_FAILED)
                        self._last_opened_profile = norm
                        print(f"[creator-search] opened-profile={norm} source=result-row")
                        return True
                    except JobCancelledError:
                        raise
                    except Exception:  # noqa: BLE001 - continue to the exact-text fallback
                        continue

        # Div-based result builds sometimes expose no row role. Only click an exact handle whose
        # ancestor contains that same handle; this is the same profile the operator had to click.
        for element in await self._find_creator_elements(norm):
            try:
                _, any_text = await self._row_text_from_ancestors(element, norm)
                if any_text is None:
                    continue
                await self._click_open(element, ErrorCode.CREATOR_OPEN_FAILED)
                self._last_opened_profile = norm
                print(f"[creator-search] opened-profile={norm} source=exact-handle")
                return True
            except JobCancelledError:
                raise
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _has_search_input(self):
        """Return the search-input locator if one is already visible, else None."""
        # The same SPA page and input are reused for the whole workbook. Checking the cached
        # locator first avoids repeating a multi-selector wait for every creator after the first.
        if self._search_box is not None:
            with contextlib.suppress(Exception):
                if await self._search_box.count() > 0 and await self._search_box.is_visible():
                    return self._search_box
        return await find_first_visible(self._page, selectors.SEARCH_INPUT, timeout_ms=500)

    async def _wait_visible_or_cancel(self, selector_candidates: list[str], timeout_ms: int):
        """Poll for a visible element in short slices so a cancel is honored fast (§6).

        Never uses one long wait_for — checks the cancel event between ~0.5s probes so a stop
        request is not delayed by a 20-30s Playwright timeout.
        """
        deadline = timeout_ms / 1000
        waited = 0.0
        while waited < deadline:
            self._check_cancel()
            element = await find_first_visible(self._page, selector_candidates, timeout_ms=500)
            if element is not None:
                return element
            await asyncio.sleep(0.2)
            waited += 0.7
        return None

    async def _visible_security_challenge(self):
        """Return a visible TikTok puzzle/captcha container, without waiting per selector."""
        for selector in SECURITY_CHALLENGE_SELECTORS:
            try:
                locator = self._page.locator(selector).first
                if await locator.count() > 0 and await locator.is_visible():
                    return locator
            except Exception:  # noqa: BLE001 -- a selector unsupported by a build is harmless
                continue
        return None

    async def _wait_for_creator_search_ready(self):
        """Wait for Creator Search, extending the wait while the user solves TikTok's puzzle.

        A normal page still keeps the short 20-second limit. Once a visible security challenge
        is detected, the page is left untouched for up to three minutes. As soon as the user
        completes it and the search input appears, lookup resumes automatically.
        """
        loop = asyncio.get_running_loop()
        normal_deadline = loop.time() + (SEARCH_READY_TIMEOUT_MS / 1000)
        # The extended deadline is created only after a visible challenge is detected. The old
        # implementation gave every US Creator route a three-minute deadline even when no puzzle
        # existed, which made a transiently remounted search input look like a frozen worker.
        challenge_deadline = None
        post_challenge_deadline = None
        announced = False

        while True:
            self._check_cancel()
            if selectors.is_affiliate_login_redirect(self._page.url):
                self.security_challenge_active = False
                raise AffiliateSessionExpiredError
            if _is_tiktok_error_page(self._page.url):
                self.security_challenge_active = False
                return None

            challenge = await self._visible_security_challenge()
            if challenge is not None:
                self.security_challenge_active = True
                post_challenge_deadline = None
                if not announced:
                    challenge_deadline = loop.time() + (
                        SECURITY_CHALLENGE_TIMEOUT_MS / 1000
                    )
                    announced = True
                    print(
                        "[security-check] TikTok puzzle detected; complete it in the browser "
                        "(waiting up to 3 minutes)"
                    )
            elif self.security_challenge_active:
                # Verification disappeared. Give the SPA its normal render window, measured from
                # completion rather than from the time the puzzle first appeared.
                self.security_challenge_active = False
                post_challenge_deadline = loop.time() + (SEARCH_READY_TIMEOUT_MS / 1000)

            # TikTok leaves the underlying search field in the DOM behind its verification modal.
            # Never treat that covered field as ready until the puzzle is gone.
            if challenge is None:
                search = await find_first_visible(
                    self._page, selectors.SEARCH_INPUT, timeout_ms=500
                )
                if search is not None:
                    self.security_challenge_active = False
                    if announced:
                        print("[security-check] completed; continuing Creator search")
                    return search

            now = loop.time()
            if challenge is not None and challenge_deadline is not None:
                if now >= challenge_deadline:
                    self.security_challenge_active = False
                    print("[security-check] timed out before the puzzle was completed")
                    return None
            elif post_challenge_deadline is not None:
                if now >= post_challenge_deadline:
                    return None
            elif now >= normal_deadline:
                return None

            await asyncio.sleep(0.2)

    async def ensure_find_creators_page(self, lane: _Lane | None = None):
        if lane is None:
            return await self._ensure_find_creators_page_active()
        token = self._lane_var.set(lane)
        try:
            return await self._ensure_find_creators_page_active()
        finally:
            self._lane_var.reset(token)

    async def _ensure_find_creators_page_active(self):
        """Reach the Find-creators search page and return its search-input locator.

        Simplified flow (spec §1, §3): reuse the current search input if present → otherwise run
        the conditional security gate, then go straight to Creator (no target-invitation) → wait up
        to ~20s for the search input. If TikTok itself bounces us elsewhere (e.g. an interstitial
        redirect), retry the creator URL exactly ONCE before giving up — no infinite retries.

        Raises:
            AffiliateSessionExpiredError: a navigation was redirected to a login/account page.
            SearchPageNotFoundError: the creator page rendered but has no search input.
        """
        search = await self._has_search_input()
        if search is not None:
            return search

        # If a puzzle is already open, do not navigate and reset it while the user is solving it.
        # Wait up to three minutes only for a puzzle that is actually visible. Merely being on
        # the US Creator route is not a verification signal: TikTok frequently remounts the
        # search input for a moment during long runs.
        if await self._visible_security_challenge() is not None:
            self.security_challenge_active = True
            search = await self._wait_for_creator_search_ready()
            if search is not None:
                return search
            raise SearchPageNotFoundError

        self._check_cancel()  # before navigating to the creator page (§6)
        await self._goto_find_creators_with_recovery()
        if _is_tiktok_error_page(self._page.url):
            raise SearchPageNotFoundError
        if selectors.is_affiliate_login_redirect(self._page.url):
            raise AffiliateSessionExpiredError

        search = await self._wait_for_creator_search_ready()
        if search is not None:
            return search

        # TikTok may auto-redirect the creator URL to an interstitial (e.g. target-invitation).
        # Let it settle briefly, then re-navigate to the creator URL exactly once.
        await asyncio.sleep(1.0)
        self._check_cancel()
        if selectors.is_affiliate_login_redirect(self._page.url):
            raise AffiliateSessionExpiredError
        await self._goto_find_creators_with_recovery()
        self._log_step("find-creators-retry")
        if _is_tiktok_error_page(self._page.url):
            raise SearchPageNotFoundError
        if selectors.is_affiliate_login_redirect(self._page.url):
            raise AffiliateSessionExpiredError

        search = await self._wait_for_creator_search_ready()
        if search is None:
            raise SearchPageNotFoundError
        return search

    async def _identify_and_extract(
        self, norm: str, result: LookupResult, lane: _Lane | None = None
    ) -> LookupResult:
        if lane is None:
            return await self._identify_and_extract_active(norm, result)
        token = self._lane_var.set(lane)
        try:
            return await self._identify_and_extract_active(norm, result)
        finally:
            self._lane_var.reset(token)

    async def _identify_and_extract_active(
        self, norm: str, result: LookupResult
    ) -> LookupResult:
        """Reliably activate the exact creator, then extract its scoped GMV and Items sold.

        TikTok currently uses both an exact autocomplete card and the input's adjacent magnifier,
        depending on account/session state. Use both safe paths and retry keyboard-style input once
        before declaring the creator missing. No page-level button or filter tab is ever clicked.
        """
        result.current_stage = STAGE_WAIT_AUTOCOMPLETE
        suggestion, ac = await self._match_autocomplete(norm)
        found, has_money = await self._creator_activation_state(norm)

        if not found and suggestion is not None:
            try:
                result.current_stage = STAGE_OPEN_CREATOR
                await self._click_open(
                    suggestion,
                    ErrorCode.AUTOCOMPLETE_CLICK_FAILED,
                    prefer_target=getattr(ac, "used_top_profile", False),
                    expected_username=norm,
                )
                print(f"[creator-search] selected={norm}")
                found, has_money = await self._wait_for_creator_activation(
                    norm, timeout_ms=SELECTED_RESULT_TIMEOUT_MS
                )
            except JobCancelledError:
                raise
            except AutocompleteClickFailedError as exc:
                # A stale/passive autocomplete node can reject a click. Continue through the
                # verified adjacent magnifier instead of converting that UI race into a failure.
                print(
                    f"[creator-search] selection-missed={norm} "
                    f"reason={_safe_exc(exc)}"
                )

        # Even after an autocomplete click, submit through the geometrically verified magnifier
        # if the exact row did not materialize. This is the missing step behind the high failure
        # rate in long jobs.
        if not found:
            result.current_stage = STAGE_WAIT_SEARCH_RESULTS
            found, has_money = await self._submit_search_with_icon(norm)

        # One keyboard-event retry handles TikTok builds that display the filled value but ignore
        # React's programmatic input event after many consecutive creators.
        if not found:
            print(f"[creator-search] retry-input={norm} method=keyboard")
            await self._retrigger_autocomplete(norm)
            retry_suggestion, retry_ac = await self._match_autocomplete(norm)
            ac.saw_panel = ac.saw_panel or retry_ac.saw_panel
            ac.saw_suggestions = ac.saw_suggestions or retry_ac.saw_suggestions
            ac.saw_username = ac.saw_username or retry_ac.saw_username

            if retry_suggestion is not None:
                try:
                    result.current_stage = STAGE_OPEN_CREATOR
                    await self._click_open(
                        retry_suggestion,
                        ErrorCode.AUTOCOMPLETE_CLICK_FAILED,
                        prefer_target=getattr(retry_ac, "used_top_profile", False),
                        expected_username=norm,
                    )
                    print(f"[creator-search] selected={norm} source=keyboard-retry")
                    found, has_money = await self._wait_for_creator_activation(
                        norm, timeout_ms=SELECTED_RESULT_TIMEOUT_MS
                    )
                except JobCancelledError:
                    raise
                except AutocompleteClickFailedError as exc:
                    print(
                        f"[creator-search] retry-selection-missed={norm} "
                        f"reason={_safe_exc(exc)}"
                    )

            if not found:
                result.current_stage = STAGE_WAIT_SEARCH_RESULTS
                found, has_money = await self._submit_search_with_icon(norm)

        if found and not has_money:
            result.current_stage = STAGE_OPEN_CREATOR
            if (
                self._last_opened_profile != norm
                and await self._click_matching_result_profile(norm)
            ):
                found, has_money = await self._wait_for_creator_activation(norm)

        if not found:
            # No exact account after both bounded submit paths is definitive for this run. Use a
            # non-transient code so the outer loop advances without touching Followers/filters.
            print(f"[creator-search] not-found={norm} reason=no-exact-result-after-retry")
            return _fail(result, ErrorCode.CREATOR_NOT_FOUND, f"'{norm}' not found")
        return await self._extract_by_row_text(norm, result, ac)

    async def _extract_by_row_text(self, norm, result, ac) -> LookupResult:
        network_result = self._apply_network_metrics(norm, result)
        if network_result is not None:
            return network_result

        # Clicking an autocomplete option returns before TikTok finishes rendering the table.
        # Poll for the actual row containing BOTH this username and a money value; otherwise the
        # old code could immediately grab the still-open suggestion card and report a false fail.
        result.current_stage = STAGE_WAIT_SEARCH_RESULTS
        self._check_cancel()
        if self._active_row_username == norm and self._active_row_text:
            row_text, any_text, saw_username = (
                self._active_row_text,
                self._active_row_text,
                True,
            )
        else:
            row_text, any_text, saw_username = await self._wait_for_creator_row_text(norm)
        if row_text is None and any_text is None:
            if saw_username:
                return _fail(result, ErrorCode.RESULT_ROW_NOT_FOUND, "username found, no readable row")
            if ac.saw_suggestions and not ac.saw_username:
                return _fail(result, ErrorCode.AUTOCOMPLETE_USERNAME_NOT_FOUND, "dropdown, no handle")
            if ac.saw_suggestions:
                return _fail(result, ErrorCode.AUTOCOMPLETE_MISMATCH, f"no match for {norm}")
            return _fail(result, ErrorCode.CREATOR_NOT_FOUND, f"'{norm}' not on results screen")

        result.current_stage = STAGE_EXTRACT_GMV
        text = row_text or any_text or ""
        gmv_raw, items_raw, items_val = self._parse_row_text(text)
        source = "row_text"

        # Fallback to the detail drawer/panel ONLY if the row text had no GMV (never times out).
        if gmv_raw is None:
            # Some TikTok accounts keep GMV out of the list until the exact creator profile is
            # opened. Perform that click automatically once, then re-read the same account-scoped
            # row/panel instead of waiting for the operator.
            if (
                self._last_opened_profile != norm
                and await self._click_matching_result_profile(norm)
            ):
                refreshed_row, refreshed_any, _ = await self._wait_for_creator_row_text(norm)
                refreshed_text = refreshed_row or refreshed_any or text
                refreshed_gmv, refreshed_items_raw, refreshed_items_val = self._parse_row_text(
                    refreshed_text
                )
                if refreshed_gmv is not None:
                    text = refreshed_text
                    gmv_raw = refreshed_gmv
                    items_raw = refreshed_items_raw
                    items_val = refreshed_items_val

        if gmv_raw is None:
            panel_raw, panel_items = await self._detail_fallback()
            if panel_raw is not None:
                gmv_raw, source = panel_raw, "detail_panel"
                if items_val is None:
                    items_val = panel_items

        self._log_gmv_row(norm, text, gmv_raw, items_raw, items_val)

        if gmv_raw is None:
            self._log_extraction_failed(ErrorCode.GMV_NOT_FOUND.value)
            return _fail(
                result,
                ErrorCode.GMV_NOT_FOUND,
                "no currency value in row and no detail GMV",
            )

        parsed = parse_gmv(gmv_raw)
        if parsed.value_type in (GmvValueType.NOT_FOUND, GmvValueType.ERROR):
            self._log_extraction_failed(ErrorCode.GMV_PARSE_ERROR.value)
            return _fail(result, ErrorCode.GMV_PARSE_ERROR, f"unparseable GMV: {gmv_raw[:60]}")

        result.gmv = parsed
        result.items_sold = items_val  # None if not found -- a missing count never fails a good GMV
        result.items_sold_raw = items_raw
        result.source = source
        result.status = (
            RowStatus.RANGE
            if parsed.value_type in (GmvValueType.RANGE_MAX, GmvValueType.OPEN_ENDED_ESTIMATE)
            else RowStatus.SUCCESS
        )
        self._log_extraction_success(source)
        return result

    def _network_metrics(self, norm: str) -> tuple[str, int | None] | None:
        """Return exact-account metrics from JSON the visible page already requested.

        Responses are scanned newest-first and strict username verification is required. This
        deliberately rejects username-less background analytics payloads, preventing stale or
        unrelated GMV from ever being assigned to the current creator.
        """
        for payload in reversed(self._captured_json):
            raw, items = extract_from_json(payload, norm, require_username=True)
            if raw is None:
                continue
            parsed = parse_gmv(raw)
            if parsed.value_type not in (GmvValueType.NOT_FOUND, GmvValueType.ERROR):
                return raw, items
        return None

    def _apply_network_metrics(self, norm: str, result: LookupResult) -> LookupResult | None:
        metrics = self._network_metrics(norm)
        if metrics is None:
            return None
        raw, items = metrics
        parsed = parse_gmv(raw)
        result.gmv = parsed
        result.items_sold = items
        result.items_sold_raw = str(items) if items is not None else None
        result.source = "network_response"
        result.status = (
            RowStatus.RANGE
            if parsed.value_type in (GmvValueType.RANGE_MAX, GmvValueType.OPEN_ENDED_ESTIMATE)
            else RowStatus.SUCCESS
        )
        self._log_gmv_row(norm, "", raw, result.items_sold_raw, items)
        self._log_extraction_success(result.source)
        return result

    async def _find_creator_elements(self, norm: str) -> list:
        """Return visible username elements; do not blindly take the first autocomplete match."""
        found: list = []
        for exact in (True, False):
            for candidate in (norm, "@" + norm):
                with contextlib.suppress(Exception):
                    loc = self._page.get_by_text(candidate, exact=exact)
                    for element in await loc.all():
                        if not await element.is_visible() or element in found:
                            continue
                        # ``exact=False`` is only a DOM discovery fallback. Verify the actual
                        # token so @arlynvibes2 can never be selected for @arlynvibes.
                        text = await element.inner_text()
                        if _text_contains_exact_username(text, norm):
                            found.append(element)
            if found:
                break
        return found

    async def _row_text_from_result_rows(self, norm: str) -> str | None:
        """Read the confirmed Creator/GMV/Items-sold table row directly when possible."""
        candidates = selectors.SEARCH_RESULT_ROWS + selectors.RESULT_ROW
        for selector in candidates:
            with contextlib.suppress(Exception):
                rows = await self._page.locator(selector).all()
                for row in rows:
                    with contextlib.suppress(Exception):
                        if not await row.is_visible():
                            continue
                        text = await row.inner_text()
                        if (
                            _text_contains_exact_username(text, norm)
                            and _MONEY_TOKEN_RE.search(text)
                        ):
                            self._active_row_username = norm
                            self._active_row_text = text
                            return text
        return None

    async def _wait_for_creator_row_text(self, norm: str):
        """Wait for a real result row instead of racing the SPA after the suggestion click."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (SEARCH_RESULTS_TIMEOUT_MS / 1000)
        last_any = None
        saw_username = False
        while loop.time() < deadline:
            self._check_cancel()

            direct = await self._row_text_from_result_rows(norm)
            if direct is not None:
                return direct, direct, True

            elements = await self._find_creator_elements(norm)
            saw_username = saw_username or bool(elements)
            for element in elements:
                row_text, any_text = await self._row_text_from_ancestors(element, norm)
                if row_text is not None:
                    self._active_row_username = norm
                    self._active_row_text = row_text
                    return row_text, any_text, True
                if any_text is not None:
                    last_any = any_text

            await asyncio.sleep(0.05)
        return None, last_any, saw_username

    async def _row_text_from_ancestors(self, element, norm: str):
        """Return the smallest table/role/div ancestor containing username and a money token."""
        any_text = None
        ancestor_selectors = [
            "xpath=ancestor::tr[1]",
            "xpath=ancestor::*[@role='row'][1]",
            *[f"xpath=ancestor::div[{level}]" for level in range(1, 17)],
        ]
        for selector in ancestor_selectors:
            self._check_cancel()
            try:
                ancestor = element.locator(selector)
                text = await ancestor.inner_text()
            except Exception:  # noqa: BLE001 -- ran out of ancestors / not readable
                continue
            if not text or not _text_contains_exact_username(text, norm):
                continue
            if _looks_like_filter_container(text):
                continue
            if any_text is None:
                any_text = text
            if _MONEY_TOKEN_RE.search(text):
                return text, any_text
        return None, any_text

    def _parse_row_text(self, text: str):
        """Return (gmv_raw, items_sold_raw, items_sold_value) from a row's text (spec 2)."""
        clean = (text or "").replace("\xa0", " ")
        money = _MONEY_TOKEN_RE.search(clean)
        gmv_raw = money.group(1).strip() if money else None
        items_raw = None
        items_val = None
        if money is not None:
            # The table order is GMV -> Items sold -> Avg. views. Keep the search close to GMV
            # and take the first standalone count after it.
            count = _COUNT_TOKEN_RE.search(clean[money.end() : money.end() + 160])
            if count is not None:
                items_raw = count.group(1).replace(" ", "")
                items_val = _parse_count(items_raw)
        return gmv_raw, items_raw, items_val

    async def _detail_fallback(self):
        """Best-effort GMV from a detail drawer/panel; returns (None, None) on timeout (no raise)."""
        try:
            panel = await self._wait_detail_ready()
        except JobCancelledError:
            raise
        except CreatorDetailTimeoutError:
            return None, None
        _, raw = await self._scoped_money(panel, selectors.GMV_LABELS, selectors.GMV_VALUES)
        items = await self._scoped_items(panel)
        return raw, items

    def _log_gmv_row(self, creator, text, gmv_raw, items_raw, items_val) -> None:
        """Print the read row to the Worker terminal (safe fields only, row_text <=500)."""
        joined = " | ".join(x.strip() for x in text.split("\n") if x.strip())[:500]
        print(f"[gmv-row] creator={creator}")
        print(f"[gmv-row] row_text={_console_safe(joined)}")
        if gmv_raw:
            parsed = parse_gmv(gmv_raw)
            # UK rows contain the pound sign. Windows' Korean cp949 console cannot encode it,
            # and a logging-only UnicodeEncodeError used to turn every valid UK result into a
            # BROWSER_ERROR. Keep the original value for parsing/storage and sanitize only logs.
            print(f"[gmv-row] gmv_raw={_console_safe(gmv_raw)}")
            print(f"[gmv-row] gmv_value={parsed.value}")
            print(f"[gmv-row] gmv_type={_GMV_TYPE_LABELS.get(parsed.value_type, 'unknown')}")
        if items_raw:
            print(f"[gmv-row] items_sold_raw={items_raw}")
            print(f"[gmv-row] items_sold_value={items_val}")

    def _log_extraction_success(self, source: str) -> None:
        print(f"[gmv-row] extraction=success source={source}")

    def _log_extraction_failed(self, reason: str) -> None:
        print(f"[gmv-row] extraction=failed reason={reason}")

    async def _match_autocomplete(self, norm: str, timeout_ms: int | None = None):
        """Wait for the dropdown and return (exact_suggestion_or_None, _AcInfo)."""
        info = _AcInfo()
        suggestions, info.saw_panel = await self._wait_autocomplete(norm, timeout_ms=timeout_ms)
        info.saw_suggestions = bool(suggestions)

        # Highest priority: once the autocomplete dropdown displays the exact account, click that
        # card immediately. Candidates are restricted to the rendered suggestion selectors; a
        # page-wide username/ancestor search can accidentally encompass the filter tabs.
        # immediately. Previously the exact username span was returned first; on the screenshot
        # DOM that span is visible but passive, so automation waited until a person clicked the
        # surrounding row. The account opened after this click is still verified against ``norm``
        # before any GMV or Items sold value is stored.
        top_profile = await self._top_creator_profile_suggestion(norm, suggestions=suggestions)
        if top_profile is not None:
            info.saw_panel = True
            info.saw_suggestions = True
            info.saw_username = True
            info.used_top_profile = True
            print(f"[creator-search] autocomplete-priority={norm} source=top-creator-row")
            return top_profile, info

        for sug in suggestions:
            try:
                suggestion_text = await sug.inner_text()
            except Exception:  # noqa: BLE001 - an unreadable dropdown entry is never clickable
                continue
            if (
                _looks_like_filter_container(suggestion_text)
                or not _text_contains_bare_username(suggestion_text, norm)
            ):
                # ``@name`` under "Hashtags, mentions and keywords" is not a creator account.
                continue
            handle = await self._username_in_scope(sug, selectors.AUTOCOMPLETE_USERNAME)
            if handle is None:
                # No dedicated handle element — fall back to the suggestion's own text (§3).
                handle = _username_from_text(suggestion_text)
            if handle is not None:
                info.saw_username = True
                if usernames_match(norm, handle):
                    return sug, info

        return None, info

    async def _top_creator_profile_suggestion(self, norm: str | None = None, *, suggestions=None):
        """Return an exact, dropdown-scoped creator card and never a page/filter ancestor."""
        if not norm:
            return None

        if suggestions is None:
            suggestions = await find_all_matching(
                self._page, selectors.render_autocomplete_suggestions(norm)
            )

        safe_suggestions = []
        for handle in suggestions:
            try:
                if not await handle.is_visible():
                    continue
                handle_text = await handle.inner_text()
            except Exception:  # noqa: BLE001 - an unreadable suggestion is never safe to click
                continue
            if (
                not _text_contains_bare_username(handle_text, norm)
                or _MONEY_TOKEN_RE.search(handle_text or "")
                or _looks_like_filter_container(handle_text)
            ):
                continue
            area = float("inf")
            with contextlib.suppress(Exception):
                box = await handle.bounding_box()
                if box:
                    area = float(box["width"]) * float(box["height"])
            safe_suggestions.append((len(handle_text.strip()), area, handle))

        # TikTok's selectors match four nested divs for one account. The outermost includes the
        # ``Creators`` heading and clicking it does nothing (or can reach adjacent controls).
        # Select the smallest exact card, which is the actual account row seen in the live DOM.
        safe_suggestions.sort(key=lambda item: (item[0], item[1]))

        for _, _, handle in safe_suggestions:

            for selector in AUTOCOMPLETE_PROFILE_CARD_ANCESTORS:
                try:
                    candidate = handle.locator(selector).first
                    if await candidate.count() == 0 or not await candidate.is_visible():
                        continue
                    text = await candidate.inner_text()
                    if (
                        not _text_contains_bare_username(text, norm)
                        or _looks_like_filter_container(text)
                    ):
                        continue
                    # Do not confuse a previously loaded GMV result row with the dropdown.
                    if _MONEY_TOKEN_RE.search(text or ""):
                        continue
                    return candidate
                except Exception:  # noqa: BLE001 - keep trying parent shapes
                    continue
            # The suggestion itself is already dropdown-scoped and exact. It is safer than
            # climbing into an unbounded generic div ancestor.
            return handle
        return None

    async def _wait_autocomplete(self, norm: str, timeout_ms: int | None = None):
        """Poll (cancel-aware) for suggestion cards; return (suggestions, saw_panel)."""
        rendered = selectors.render_autocomplete_suggestions(norm)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + ((timeout_ms or AUTOCOMPLETE_TIMEOUT_MS) / 1000)
        saw_panel = False
        while loop.time() < deadline:
            self._check_cancel()  # a stop must work while waiting for the dropdown (§6)
            suggestions = await find_all_matching(self._page, rendered)
            visible = []
            for suggestion in suggestions:
                with contextlib.suppress(Exception):
                    if await suggestion.is_visible():
                        visible.append(suggestion)
            if visible:
                return visible, True

            # Observe panel presence without a sequential wait across broad fallback selectors.
            if not saw_panel:
                for selector in selectors.AUTOCOMPLETE_PANEL:
                    with contextlib.suppress(Exception):
                        panel = self._page.locator(selector).first
                        if await panel.count() > 0 and await panel.is_visible():
                            saw_panel = True
                            break

            await asyncio.sleep(0.04)
        return [], saw_panel

    async def _click_open(
        self,
        target,
        click_error,
        *,
        prefer_target: bool = False,
        expected_username: str | None = None,
    ) -> None:
        """Click ``target`` to open the creator; raise the appropriate open/click error."""
        el = target if prefer_target else None
        if el is None and click_error is ErrorCode.AUTOCOMPLETE_CLICK_FAILED:
            # The suggestion is already visible when this method is called, so only inspect
            # currently rendered descendants/ancestors. Waiting on result-row-only selectors
            # here used to add 350+ ms to every creator before the actual suggestion click.
            el = await find_first_visible(
                target,
                AUTOCOMPLETE_PROFILE_CARD_ANCESTORS,
                timeout_ms=0,
            )
            if el is None:
                el = await find_first_visible(
                    target,
                    selectors.AUTOCOMPLETE_CLICK_TARGET,
                    timeout_ms=0,
                )
            if el is None:
                el = target
        if el is None:
            el = await find_first_visible(target, selectors.RESULT_ROW_CLICK_TARGET, timeout_ms=350)
        if el is None:
            el = target
        if expected_username is not None:
            try:
                candidate_text = await el.inner_text()
            except Exception as exc:  # noqa: BLE001 - an unverifiable target must never be clicked
                raise AutocompleteClickFailedError("autocomplete target has no readable username") from exc
            if (
                not _text_contains_bare_username(candidate_text, expected_username)
                or _looks_like_filter_container(candidate_text)
            ):
                raise AutocompleteClickFailedError(
                    f"autocomplete target is not a safe exact card for '{expected_username}'"
                )
        try:
            with contextlib.suppress(Exception):
                await el.scroll_into_view_if_needed()
            try:
                await el.click(timeout=2_000)
            except TypeError:  # minimal test doubles / older Playwright-compatible wrappers
                await el.click()
        except Exception as first_exc:  # noqa: BLE001
            # A re-render can temporarily place a transparent/stale layer over the right option.
            # Try the exact matched element once with a forced click before the outer retry
            # re-resolves the whole dropdown.
            try:
                try:
                    await el.click(timeout=2_000, force=True)
                except TypeError:
                    await el.click()
                return
            except Exception as exc:  # noqa: BLE001
                if click_error is ErrorCode.AUTOCOMPLETE_CLICK_FAILED:
                    raise AutocompleteClickFailedError(_safe_exc(exc or first_exc)) from exc
                raise CreatorOpenFailedError(_safe_exc(exc or first_exc)) from exc

    async def _username_in_scope(self, scope, selector_list) -> str | None:
        """Extract a username from WITHIN a scope (row or suggestion), never the page, §4."""
        for selector in selector_list:
            try:
                loc = scope.locator(selector).first
                if await loc.count() == 0:
                    continue
                txt = (await loc.inner_text()).strip()
            except Exception as exc:  # noqa: BLE001
                raise SelectorError(f"{selector}: {_safe_exc(exc)}") from exc
            handle = _username_from_text(txt)
            if handle:
                return handle
        return None

    async def _scoped_money(self, root, label_selectors, value_selectors) -> tuple[bool, str | None]:
        """Return (label_present, raw_value) searching ONLY inside ``root`` (row/panel), §6."""
        if root is None:
            return (False, None)
        label = await find_first_visible(root, label_selectors, timeout_ms=800)
        if label is None:
            return (False, None)
        value = await self._first_matching_text(root, value_selectors, _looks_like_money)
        return (True, value)

    async def _scoped_items(self, root) -> int | None:
        if root is None:
            return None
        label = await find_first_visible(root, selectors.ITEMS_SOLD_LABELS, timeout_ms=500)
        if label is None:
            return None
        text = await self._first_matching_text(root, selectors.ITEMS_SOLD_VALUES, _looks_like_count)
        return _parse_count(text) if text else None

    async def _first_matching_text(self, root, value_selectors, predicate) -> str | None:
        for selector in value_selectors:
            try:
                elements = await root.locator(selector).all()
            except Exception as exc:  # noqa: BLE001
                raise SelectorError(f"value {selector}: {_safe_exc(exc)}") from exc
            for el in elements:
                with contextlib.suppress(Exception):
                    if await el.is_visible():
                        txt = (await el.inner_text()).strip()
                        if txt and predicate(txt):
                            return txt
        return None

    async def _wait_detail_ready(self):
        """Poll (cancel-aware) up to 15s for a detail panel / GMV label to appear (§5)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (DETAIL_TIMEOUT_MS / 1000)
        while loop.time() < deadline:
            self._check_cancel()  # a stop must work even while waiting on the detail panel (§6)
            panel = await find_first_visible(self._page, selectors.DETAIL_PANEL, timeout_ms=0)
            if panel is not None:
                return panel
            label = await find_first_visible(self._page, selectors.GMV_LABELS, timeout_ms=0)
            if label is not None:
                return self._page  # GMV rendered without a distinct panel — scope to the page
            await asyncio.sleep(0.05)
        raise CreatorDetailTimeoutError(
            f"detail panel/GMV label not shown within {DETAIL_TIMEOUT_MS}ms"
        )

    async def close(self) -> None:
        print(
            f"[browser-session] action=close job_id={self.profile.runtime_job_id or '-'} "
            f"profile={self.profile.profile_code} user_data_dir={self.profile.storage_root}"
        )
        for lane in self._lanes:
            with contextlib.suppress(Exception):
                if lane.page is not None:
                    await lane.page.close()
        with contextlib.suppress(Exception):
            if self._context is not None:
                await self._context.close()
        with contextlib.suppress(Exception):
            if self._pw is not None:
                await self._pw.stop()
        self._lane_queue = None
        self._lanes = []

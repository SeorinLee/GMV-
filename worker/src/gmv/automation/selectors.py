"""Ordered selector fallback lists for the Affiliate Center (spec §11, D10).

The real DOM is NOT verified from the dev environment. Every locator is an ordered list of
candidates; the driver tries each until one matches. When the live DOM is confirmed, correct
the *first* entry in each list here — this is the single place to update.
"""

from __future__ import annotations

# Search box on the "Find creators" screen.
SEARCH_INPUT = [
    "[data-e2e='search-creator-input']",
    "input[placeholder*='Search' i]",
    "input[placeholder*='creator' i]",
    "input[type='search']",
    "input",  # last-resort (legacy used input.first)
]

# ---- Search-result rows and creator detail (UNVERIFIED against the live DOM, D10) ----
# Every list below is an ordered set of *individual* candidates. They are NEVER comma-joined
# (CSS + Playwright text= must not mix in one string, spec §15); each is evaluated on its own
# and only genuinely visible elements are used. Correct the FIRST entry once the live DOM is
# confirmed via tools/inspect_creator_result.py.

# A single creator result row/card in the search results list.
RESULT_ROWS = [
    "[data-e2e='creator-card']",
    "[data-e2e='search-card']",
    "[class*='creator-item']",
    "[class*='CreatorCard']",
    "[class*='search-result']",
    "[data-result-username]",
    "[role='row']",
    "li[class*='creator']",
]
RESULT_ROW = RESULT_ROWS  # backwards-compatible alias

# The unique username/handle within a result row (used for exact verification, spec §14).
RESULT_USERNAME = [
    "[data-result-username]",
    "[data-e2e='creator-unique-id']",
    "[data-e2e='creator-username']",
    "[class*='unique-id']",
    "[class*='uniqueId']",
    "[class*='username']",
    "[class*='handle']",
    "a[href*='/@']",
]

# ---- Autocomplete search dropdown (the REAL results surface, confirmed by live screenshot) ----
# Typing a username shows a dropdown of suggestion cards under the search box (not a table).
# UNVERIFIED exact classes — ordered fallbacks; correct the first entry once confirmed.

AUTOCOMPLETE_PANEL = [
    "div[role='listbox']",
    "div[class*='popover']",
    "div[class*='dropdown']",
    "div[class*='suggest']",
    "div[class*='search']",
    "div:has-text('Creators')",
]

# Suggestion-card templates. @@USERNAME@@ -> "@name", @@USERNAME_NO_AT@@ -> "name".
# Rendered per-search via ``render_autocomplete_suggestions(username)`` — NEVER comma-joined.
_AUTOCOMPLETE_SUGGESTION_TEMPLATES = [
    "[role='option']",
    "a:has-text('@@USERNAME_NO_AT@@')",
    "div[class*='item']:has-text('@@USERNAME_NO_AT@@')",
    "div[class*='creator']:has-text('@@USERNAME_NO_AT@@')",
]

# Username/handle text WITHIN one suggestion card (used for exact verification).
AUTOCOMPLETE_USERNAME = [
    "[class*='unique']",
    "[class*='username']",
    "[class*='handle']",
    "a[href*='/@']",
    "span",
]

# Clickable element within a suggestion (priority: handle link -> option -> the card itself).
AUTOCOMPLETE_CLICK_TARGET = [
    "a[href*='/@']",
    "[role='option']",
    "button",
]


def render_autocomplete_suggestions(username: str) -> list[str]:
    """Return suggestion selectors with the username substituted (spec §1 dynamic locator)."""
    handle = (username or "").strip().lstrip("@")
    return [
        tpl.replace("@@USERNAME@@", "@" + handle).replace("@@USERNAME_NO_AT@@", handle)
        for tpl in _AUTOCOMPLETE_SUGGESTION_TEMPLATES
    ]


# Clickable element to open a creator's detail (priority: explicit button -> handle link -> row).
RESULT_ROW_CLICK_TARGET = [
    "text=/view details/i",
    "text=/view profile/i",
    "text=/상세/",
    "[data-e2e='view-details']",
    "button[class*='detail']",
    "a[href*='/@']",
    "a[href*='creator']",
]

# ---- Search results TABLE (GMV/Items sold shown inline per row — confirmed by screenshot) ----
# After selecting a creator, a "Search results" table appears whose columns are roughly
# Creator | Video | GMV | Items sold | Avg. video views. Values live in the SAME row as the
# creator, so we read them there before ever waiting for a detail drawer. UNVERIFIED classes —
# rely on header/row text as much as class names; correct the first entry once confirmed.

SEARCH_RESULTS_SECTION = [
    "[data-e2e='search-result']",
    "[class*='search-result']",
    "section:has-text('Search results')",
    "div:has-text('Search results')",
    "table",
]
SEARCH_RESULT_TABLE = [
    "table",
    "[role='table']",
    "[class*='table']",
    "[class*='list']",
]
SEARCH_RESULT_ROWS = [
    "tbody tr",
    "[role='row']",
    "[class*='table-row']",
    "[class*='result-row']",
    "[class*='creator-row']",
    "[data-e2e='creator-card']",
    "li[class*='creator']",
]
SEARCH_RESULT_HEADER_CELLS = [
    "thead th",
    "[role='columnheader']",
    "[class*='header'] [class*='cell']",
]
SEARCH_RESULT_CREATOR_CELL = [
    "[class*='creator']",
    "[class*='username']",
    "[class*='unique']",
    "a[href*='/@']",
    "td:first-child",
]
SEARCH_RESULT_DISPLAY_NAME = [
    "[class*='nickname']",
    "[class*='display-name']",
    "[class*='displayName']",
    "[class*='name']",
]
SEARCH_RESULT_GMV_CELL = [
    "[data-e2e='creator-gmv']",
    "[class*='gmv']",
    "[class*='GMV']",
    "[class*='revenue']",
]
SEARCH_RESULT_ITEMS_SOLD_CELL = [
    "[data-e2e='items-sold']",
    "[class*='items-sold']",
    "[class*='itemsSold']",
    "[class*='sold']",
]
SEARCH_RESULT_PPS = [
    "[class*='pps']",
    "[class*='rating']",
    "[class*='score']",
]
SEARCH_RESULT_AUDIENCE = [
    "[class*='audience']",
    "[class*='follower']",
    "[class*='demograph']",
]
SEARCH_RESULT_STATUS_BADGE = [
    "[class*='status']",
    "[class*='badge']",
    "[class*='invited']",
    "[class*='tag']",
]
SEARCH_RESULT_CATEGORY = [
    "[class*='category']",
    "[class*='industry']",
]


# The detail drawer / dialog / panel shown after opening a creator.
DETAIL_PANEL = [
    "[data-e2e='creator-detail']",
    "[role='dialog']",
    "[class*='drawer']",
    "[class*='Drawer']",
    "[class*='detail-panel']",
    "[class*='DetailPanel']",
    "[class*='creator-detail']",
]

# GMV label text and the value element near it (scoped to the detail panel/card, spec §6).
GMV_LABELS = [
    "text=/gross merchandise value/i",
    "text=/\\bGMV\\b/",
    "text=/revenue/i",
    "text=/sales amount/i",
]
GMV_VALUES = [
    "[data-e2e='creator-gmv']",
    "[class*='gmv-value']",
    "[class*='gmvValue']",
    "[class*='gmv']",
    "[class*='GMV']",
    "[class*='metric-value']",
    "[class*='value']",
]
GMV_CELL = GMV_VALUES  # backwards-compatible alias

# Items-sold label + value (optional; missing items must not fail a good GMV, spec §7).
ITEMS_SOLD_LABELS = [
    "text=/items sold/i",
    "text=/products sold/i",
    "text=/\\borders\\b/i",
    "text=/\\bsales\\b/i",
]
ITEMS_SOLD_VALUES = [
    "[data-e2e='items-sold']",
    "[class*='items-sold']",
    "[class*='itemsSold']",
    "[class*='sold']",
    "[class*='metric-value']",
    "[class*='value']",
]

# Loading indicator (waited on: appears then disappears).
LOADING = [
    "[class*='loading']",
    "[class*='spinner']",
    "[data-e2e='loading']",
]

# "No results" / empty-state message.
NO_RESULT = [
    "text=/no results/i",
    "text=/no creators? found/i",
    "[class*='empty']",
]

# "Find creators" navigation entry, used to reach the search screen (spec §16).
# UNVERIFIED against the live DOM — ordered fallback, first entry corrected once confirmed.
FIND_CREATORS_MENU = [
    "[data-e2e='find-creators-nav']",
    "a[href*='find-creators']",
    "a[href*='creator']",
    "text=/find creators/i",
    "text=/크리에이터 찾기/",
]

# Elements confirming the Affiliate Center entry (target-invitation) has actually rendered.
# SPA pages settle late, so we wait on a real DOM element (not networkidle). UNVERIFIED — the
# ordered fallback below is intentionally broad; correct the first entry once the DOM is known.
AFFILIATE_ENTRY_READY = [
    "[data-e2e='connection-page']",
    "[class*='target-invitation']",
    "[class*='connection']",
    "[class*='invitation']",
    "main",
    "[role='main']",
]

# ---- Seller Center login screen (where the user actually authenticates) ----
# The user logs in at seller-us.tiktok.com, NOT at the Affiliate Center. The DOM below is
# UNVERIFIED against the live site — ordered fallbacks; correct the first entry once confirmed.

# Elements that only appear once the Seller Center session is authenticated.
SELLER_LOGGED_IN = [
    "[data-e2e='seller-home']",
    "[class*='shop-name']",
    "[class*='account-info']",
    "[class*='seller-header']",
    "[class*='side-nav']",
]

# The login/sign-in form or its inputs (visible when a session is required).
SELLER_LOGIN_FORM = [
    "[data-e2e='login-form']",
    "form[class*='login']",
    "input[name='email']",
    "input[type='password']",
    "text=/log in/i",
    "text=/sign in/i",
    "text=/로그인/",
]

# Loading / skeleton indicators — a page that is only loading is NOT logged in yet.
SELLER_LOADING = [
    "[class*='loading']",
    "[class*='skeleton']",
    "[class*='spinner']",
    "[data-e2e='loading']",
]

# URL fragments that indicate a genuinely logged-out / expired / auth-gate page.
# NOTE: "/account/register" is deliberately NOT listed — it is the current *normal* Seller
# Center login-entry URL, so a page sitting there must not be judged expired/failed on the URL
# alone (login state is confirmed by the SELLER_* selectors instead).
LOGGED_OUT_URL_MARKERS = [
    "ttp_session_expire",
    "/account/login",
    "/login",
    "/signup",
    "/passport",
    "accounts.tiktok.com",
]


# During an active GMV job on the Affiliate Center, being sent to ANY Seller-account/auth page
# (including /account/register) means the session was lost — the opposite of the login flow,
# where /account/register is the normal entry. URL judgement is therefore context-specific (§7).
AFFILIATE_LOGOUT_URL_MARKERS = [
    *LOGGED_OUT_URL_MARKERS,
    "/account/register",
    "/account",
]


def is_logged_out_url(url: str) -> bool:
    """True if the URL clearly indicates an expired / auth-gate session, in the LOGIN flow.

    Intentionally conservative: only unambiguous logout/auth-gate hosts count. The Seller Center
    login-entry path (/account/register) is excluded so it is never mistaken for a failure here.
    """
    low = (url or "").lower()
    return any(marker in low for marker in LOGGED_OUT_URL_MARKERS)


def is_affiliate_login_redirect(url: str) -> bool:
    """True if an Affiliate-Center URL indicates the session was lost (GMV-job context, §7).

    Stricter than :func:`is_logged_out_url`: here a redirect to any Seller ``/account`` page —
    including ``/account/register`` — means the session expired mid-job and must be re-logged.
    """
    low = (url or "").lower()
    return any(marker in low for marker in AFFILIATE_LOGOUT_URL_MARKERS)

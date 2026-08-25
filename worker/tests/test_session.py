"""Automation-layer tests for creator navigation + row-text GMV extraction.

The live Playwright DOM is UNVERIFIED (D10); these use a minimal fake page/locator that mimics
only the surface the session touches, so the navigation + extraction *logic* is covered without a
real browser. The extraction mirrors the proven approach: find the creator username via
get_by_text, walk up ancestor divs to the smallest container holding the username AND a "$", and
parse GMV + Items sold from that row's text.
"""

import asyncio
import re

import pytest

from gmv.automation import selectors
from gmv.automation.session import (
    AUTOCOMPLETE_TIMEOUT_MS,
    SEARCH_ICON_RELATIVE_SELECTORS,
    SEARCH_ICON_RESULT_TIMEOUT_MS,
    SECURITY_CHALLENGE_SELECTORS,
    AffiliateSessionExpiredError,
    SearchPageNotFoundError,
    TikTokAffiliateSession,
    _console_safe,
    find_all_matching,
)
from gmv.config import get_default_profile
from gmv.models import ErrorCode, GmvValueType, JobCancelledError, LookupResult, RowStatus


@pytest.fixture(autouse=True)
def _fast_timeouts(monkeypatch):
    """Keep the polling loops instant so the fake-DOM tests don't sit through real timeouts."""

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("gmv.automation.session.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("gmv.automation.session.AUTOCOMPLETE_TIMEOUT_MS", 200)
    monkeypatch.setattr("gmv.automation.session.DETAIL_TIMEOUT_MS", 200)
    monkeypatch.setattr("gmv.automation.session.SEARCH_ICON_RESULT_TIMEOUT_MS", 200)
    monkeypatch.setattr("gmv.automation.session.SEARCH_RESULTS_TIMEOUT_MS", 200)
    monkeypatch.setattr("gmv.automation.session.SELECTED_RESULT_TIMEOUT_MS", 200)
    monkeypatch.setattr("gmv.automation.session.SEARCH_READY_TIMEOUT_MS", 200)
    monkeypatch.setattr("gmv.automation.session.SEARCH_TIMEOUT_MS", 200)
    monkeypatch.setattr("gmv.automation.session.SECURITY_CHALLENGE_APPEAR_TIMEOUT_MS", 0)
    monkeypatch.setattr("gmv.automation.session.SECURITY_RECOVERY_APPEAR_TIMEOUT_MS", 0)


def _session():
    return TikTokAffiliateSession(get_default_profile(), headless=True)


def test_lane_context_keeps_legacy_attributes_isolated():
    session = _session()
    default_page = object()
    lane_page = object()
    session._page = default_page
    lane = type(session._default_lane)(page=lane_page)

    token = session._lane_var.set(lane)
    try:
        session._captured_json.append({"lane": True})
        session._search_box = "lane-search"
        assert session._page is lane_page
    finally:
        session._lane_var.reset(token)

    assert session._page is default_page
    assert session._captured_json == []
    assert lane.captured_json == [{"lane": True}]
    assert lane.search_box == "lane-search"


def _run(coro):
    return asyncio.run(coro)


# ---- Fakes for navigation tests (ensure_find_creators_page) ----


class FakeElement:
    def __init__(self, username: str, text: str):
        self.username = username
        self.text = text

    async def inner_text(self):
        return self.text

    async def is_visible(self):
        return True

    def locator(self, selector: str):
        return FakeLocator([])


class FakeLocator:
    def __init__(self, elements: list):
        self._elements = elements

    @property
    def first(self):
        return self

    async def all(self):
        return list(self._elements)

    async def count(self):
        return len(self._elements)

    async def is_visible(self):
        return bool(self._elements)

    async def inner_text(self):
        return self._elements[0].text if self._elements else ""

    async def wait_for(self, state="visible", timeout=0):
        if not self._elements:
            raise TimeoutError("not visible")

    async def click(self):
        return None


class FakePage:
    def __init__(self, rows: list):
        self.rows = rows

    def locator(self, selector: str):
        if selector == selectors.RESULT_ROWS[0]:
            return FakeLocator(list(self.rows))
        return FakeLocator([])


class FlowPage:
    """Records navigations; SEARCH_INPUT visible only on the creator URL (spec §1, §3)."""

    def __init__(
        self,
        *,
        creator_has_search=True,
        redirect_to=None,
        security_challenge_polls=0,
        error_after_puzzle=False,
    ):
        self.urls: list = []
        self.url = "about:blank"
        self._creator_has_search = creator_has_search
        self._redirect_to = redirect_to
        self._security_challenge_polls = security_challenge_polls
        self._error_after_puzzle = error_after_puzzle
        self._puzzle_was_seen = False
        self._post_puzzle_error_emitted = False
        self._p = get_default_profile()

    async def goto(self, url, wait_until=None):
        self.urls.append(url)
        self.url = self._redirect_to or url

    def on(self, *a, **k):
        return None

    def locator(self, selector):
        challenge_route = self.url in {self._p.login_success_url, self._p.find_creators_url}
        if (
            selector in SECURITY_CHALLENGE_SELECTORS
            and challenge_route
            and self._security_challenge_polls > 0
        ):
            self._security_challenge_polls -= 1
            self._puzzle_was_seen = True
            return FakeLocator([FakeElement("puzzle", "puzzle")])
        if (
            selector in SECURITY_CHALLENGE_SELECTORS
            and challenge_route
            and self._error_after_puzzle
            and self._puzzle_was_seen
            and not self._post_puzzle_error_emitted
        ):
            self.url = "https://affiliate-us.tiktok.com/errorpage"
            self._post_puzzle_error_emitted = True
        visible = (
            self.url == self._p.find_creators_url
            and selector in selectors.SEARCH_INPUT
            and self._creator_has_search
            and self._security_challenge_polls <= 0
        )
        return FakeLocator([FakeElement("x", "x")] if visible else [])


def test_find_all_matching_checks_each_selector_individually():
    async def go():
        page = FakePage([FakeElement("x", "x")])
        found = await find_all_matching(page, selectors.RESULT_ROWS)
        assert len(found) == 1

    asyncio.run(go())


def test_ensure_without_puzzle_opens_creator_immediately():
    async def go():
        session = _session()
        page = FlowPage()
        session._page = page
        search = await session.ensure_find_creators_page()
        assert search is not None
        assert page.urls == [
            session.profile.login_success_url,
            session.profile.find_creators_url,
        ]
        assert session.profile.affiliate_entry_url not in page.urls

    asyncio.run(go())


def test_ensure_raises_session_expired_on_login_redirect():
    async def go():
        session = _session()
        session._page = FlowPage(redirect_to="https://seller-us.tiktok.com/account/register")
        with pytest.raises(AffiliateSessionExpiredError):
            await session.ensure_find_creators_page()

    asyncio.run(go())


def test_ensure_raises_search_page_not_found_when_creator_has_no_search():
    async def go():
        session = _session()
        session._page = FlowPage(creator_has_search=False)
        with pytest.raises(SearchPageNotFoundError):
            await session.ensure_find_creators_page()

    asyncio.run(go())


def test_ensure_waits_for_manual_security_puzzle_then_resumes_without_renavigation():
    async def go():
        session = _session()
        page = FlowPage(security_challenge_polls=2)
        page.url = session.profile.find_creators_url
        session._page = page
        search = await session.ensure_find_creators_page()
        assert search is not None
        assert page.urls == []

    asyncio.run(go())


def test_lookup_start_waits_for_puzzle_before_opening_creator():
    async def go():
        session = _session()
        page = FlowPage(security_challenge_polls=2)
        session._page = page
        search = await session.ensure_find_creators_page()
        assert search is not None
        assert page.urls == [
            session.profile.login_success_url,
            session.profile.find_creators_url,
        ]
        assert session.profile.affiliate_entry_url not in page.urls

    asyncio.run(go())


def test_post_puzzle_error_recovers_without_retry_loop_then_opens_creator():
    async def go():
        session = _session()
        page = FlowPage(security_challenge_polls=2, error_after_puzzle=True)
        session._page = page
        search = await session.ensure_find_creators_page()
        assert search is not None
        assert page.urls == [
            session.profile.login_success_url,
            session.profile.login_success_url,
            session.profile.find_creators_url,
        ]
        assert session.profile.affiliate_entry_url not in page.urls

    asyncio.run(go())


def test_search_creator_maps_session_expired_to_error_code():
    async def go():
        session = _session()
        session._page = FlowPage(redirect_to="https://seller-us.tiktok.com/account/register")
        result = await session.search_creator("someone")
        assert result.status is RowStatus.FAILED
        assert result.error_code is ErrorCode.SESSION_EXPIRED

    asyncio.run(go())


def test_search_creator_propagates_cancel():
    async def go():
        session = _session()
        session._page = FlowPage()
        event = asyncio.Event()
        event.set()
        session.cancel_event = event
        with pytest.raises(JobCancelledError):
            await session.search_creator("someone")

    asyncio.run(go())


# ---- Fake DOM for row-text extraction (get_by_text + ancestor walk) ----


class Node:
    """An element that is also a container + a page that supports get_by_text."""

    def __init__(self, text="", visible=True):
        self.text = text
        self._visible = visible
        self.clicked = False
        self._groups: list = []  # (set_of_selectors, [child Node])
        self._text_nodes: list = []

    def add(self, selectors_list, elements):
        self._groups.append((set(selectors_list), list(elements)))
        return self

    def add_text(self, node):
        self._text_nodes.append(node)
        return self

    def get_by_text(self, text, exact=True):
        matched = [n for n in self._text_nodes if (n.text == text if exact else text in n.text)]
        return _Loc(matched)

    @property
    def first(self):
        return self

    async def all(self):
        return [self]

    async def count(self):
        return 1

    async def inner_text(self, timeout=0):
        return self.text

    async def is_visible(self):
        return self._visible

    async def wait_for(self, state="visible", timeout=0):
        if not self._visible:
            raise TimeoutError("not visible")

    async def click(self):
        self.clicked = True

    async def press(self, _key):
        return None

    async def fill(self, value):
        self.text = value

    def locator(self, selector):
        for sels, els in self._groups:
            if selector in sels:
                return _Loc(els)
        return _Loc([])


class _Loc:
    def __init__(self, elements):
        self._els = elements

    @property
    def first(self):
        return self._els[0] if self._els else Node(visible=False)

    async def all(self):
        return list(self._els)

    async def count(self):
        return len(self._els)


class _AncLoc:
    def __init__(self, text):
        self._t = text

    async def inner_text(self, timeout=0):
        if self._t is None:
            raise RuntimeError("no such ancestor")
        return self._t


class TextNode(Node):
    """A username text element whose ancestor::div[N] resolves to a preset text."""

    def __init__(self, text, ancestors):
        super().__init__(text=text)
        self._ancestors = ancestors  # {level: text}

    def locator(self, selector):
        m = re.search(r"ancestor::div\[(\d+)\]", selector)
        if m:
            return _AncLoc(self._ancestors.get(int(m.group(1))))
        return super().locator(selector)


class _BadNode(Node):
    async def click(self):
        raise RuntimeError("suggestion not clickable")


ROW_FULL = "\n".join([
    "yourboybigmike",
    "M I K E",
    "PPS: 4.7/5.0",
    "Beauty & Personal Care, +2",
    "10K, Female 65%, 35-44",
    "$150.1K",
    "4.8K",
])


def make_result_page(username, ancestors, *, detail_gmv=None):
    page = Node()
    page.add_text(TextNode(username, ancestors))
    if detail_gmv is not None:
        panel = Node("detail", visible=True)
        panel.add([selectors.GMV_LABELS[1]], [Node("GMV")])
        panel.add([selectors.GMV_VALUES[0]], [Node(detail_gmv)])
        page.add([selectors.DETAIL_PANEL[0]], [panel])
    return page


def make_suggestion_page(handle, *, click_bad=False):
    page = Node()
    node = _BadNode(text=handle) if click_bad else Node(text=handle)
    node.add([selectors.AUTOCOMPLETE_USERNAME[0]], [Node(handle)])
    page.add([selectors.render_autocomplete_suggestions("x")[0]], [node])
    return page


def _extract(session, username="yourboybigmike"):
    return session._identify_and_extract(username, LookupResult(normalized_username=username))


def test_row_text_gmv_150k_conversion():
    session = _session()
    session._page = make_result_page("yourboybigmike", {2: "yourboybigmike", 3: ROW_FULL})
    r = _run(_extract(session))
    assert r.status is RowStatus.SUCCESS
    assert r.gmv.value == 150100
    assert r.source == "row_text"


def test_exact_network_response_skips_dom_wait_and_extracts_both_metrics():
    session = _session()
    session._page = Node()
    session._captured_json = [
        {
            "data": {
                "creators": [
                    {"unique_id": "someone_else", "gmv_str": "$99K", "items_sold": 999},
                    {"unique_id": "alpha", "gmv_str": "$12.5K", "items_sold": 42},
                ]
            }
        }
    ]
    result = LookupResult(normalized_username="alpha")

    applied = session._apply_network_metrics("alpha", result)

    assert applied is result
    assert result.status is RowStatus.SUCCESS
    assert result.gmv.value == 12500
    assert result.items_sold == 42
    assert result.source == "network_response"


def test_network_response_never_uses_another_creator_or_username_less_payload():
    session = _session()
    session._captured_json = [
        {"unique_id": "someone_else", "gmv_str": "$99K", "items_sold": 999},
        {"gmv_str": "$88K", "items_sold": 888},
    ]

    assert session._network_metrics("alpha") is None


def test_row_text_items_sold_4800_conversion():
    session = _session()
    session._page = make_result_page("yourboybigmike", {2: "yourboybigmike", 3: ROW_FULL})
    r = _run(_extract(session))
    assert r.items_sold == 4800


def test_confirmed_screenshot_values_expand_to_numbers():
    row = "\n".join(["yourboybigmike", "M I K E", "PPS: 4.7/5.0", "$148.4K", "4.7K"])
    session = _session()
    session._page = make_result_page("yourboybigmike", {2: "yourboybigmike", 3: row})
    r = _run(_extract(session))
    assert r.status is RowStatus.SUCCESS
    assert r.gmv.value == 148400
    assert r.items_sold == 4700


def test_flattened_table_row_extracts_only_gmv_and_first_count_after_it():
    row = "yourboybigmike M I K E PPS: 4.7/5.0 10.1K Female 65% 35-44 $148.4K 4.7K 22.3K"
    session = _session()
    session._page = make_result_page("yourboybigmike", {2: "yourboybigmike", 3: row})
    r = _run(_extract(session))
    assert r.gmv.value == 148400
    assert r.items_sold == 4700


def test_items_sold_range_uses_maximum_value():
    row = "\n".join(["yourboybigmike", "$148.4K", "0-100"])
    session = _session()
    session._page = make_result_page("yourboybigmike", {2: "yourboybigmike", 3: row})
    r = _run(_extract(session))
    assert r.gmv.value == 148400
    assert r.items_sold_raw == "0-100"
    assert r.items_sold == 100


def test_items_sold_abbreviated_range_uses_expanded_maximum():
    row = "\n".join(["yourboybigmike", "$2M", "0-1.2K"])
    session = _session()
    session._page = make_result_page("yourboybigmike", {2: "yourboybigmike", 3: row})
    r = _run(_extract(session))
    assert r.items_sold == 1200


def test_items_found_within_1_to_3_lines_after_gmv():
    row = "\n".join(["yourboybigmike", "$150.1K", "not a number", "4.8K"])  # items 2 lines later
    session = _session()
    session._page = make_result_page("yourboybigmike", {2: "yourboybigmike", 3: row})
    r = _run(_extract(session))
    assert r.items_sold == 4800


def test_gmv_found_without_items_is_success():
    row = "\n".join(["yourboybigmike", "M I K E", "$150.1K"])  # no items line
    session = _session()
    session._page = make_result_page("yourboybigmike", {2: "yourboybigmike", 3: row})
    r = _run(_extract(session))
    assert r.status is RowStatus.SUCCESS
    assert r.items_sold is None


def test_range_gmv_records_upper_bound():
    row = "\n".join(["yourboybigmike", "$0-$5K"])
    session = _session()
    session._page = make_result_page("yourboybigmike", {2: "yourboybigmike", 3: row})
    r = _run(_extract(session))
    assert r.status is RowStatus.RANGE
    assert r.gmv.value == 5000
    assert r.gmv.value_type is GmvValueType.RANGE_MAX  # "range_upper"


def test_ancestor_walk_picks_smallest_row_with_username_and_dollar():
    # Levels 2 and 3 hold the username but no $; level 4 is the row with the price.
    ancestors = {
        2: "yourboybigmike",
        3: "yourboybigmike\nM I K E",
        4: ROW_FULL,
        5: "wrapper " + ROW_FULL,
    }
    session = _session()
    session._page = make_result_page("yourboybigmike", ancestors)
    r = _run(_extract(session))
    assert r.status is RowStatus.SUCCESS
    assert r.gmv.value == 150100


def test_row_text_success_does_not_wait_for_detail_drawer():
    # No detail panel exists; if the code waited for one it would fail. Success => row_text used.
    session = _session()
    session._page = make_result_page("yourboybigmike", {2: "yourboybigmike", 3: ROW_FULL})
    r = _run(_extract(session))
    assert r.status is RowStatus.SUCCESS
    assert r.source == "row_text"


def test_row_text_terminal_log_has_key_fields(capsys):
    session = _session()
    session._page = make_result_page("yourboybigmike", {2: "yourboybigmike", 3: ROW_FULL})
    _run(_extract(session))
    out = capsys.readouterr().out
    assert "[gmv-row] gmv_raw=$150.1K" in out
    assert "gmv_value=150100" in out
    assert "items_sold_value=4800" in out
    assert "extraction=success source=row_text" in out


def test_uk_pound_gmv_log_is_safe_on_windows_cp949(monkeypatch):
    import io
    import sys

    session = _session()
    output = io.BytesIO()
    cp949_stream = io.TextIOWrapper(output, encoding="cp949")
    monkeypatch.setattr(sys, "stdout", cp949_stream)

    session._log_gmv_row(
        "nimrxhh",
        "nimrxhh\n£4.2K\n236",
        "£4.2K",
        "236",
        236,
    )
    cp949_stream.flush()
    logged = output.getvalue().decode("cp949")

    assert r"gmv_raw=\xa34.2K" in logged
    assert "gmv_value=4200" in logged


def test_username_not_on_screen_is_creator_not_found():
    session = _session()
    session._page = Node()  # no text nodes
    r = _run(_extract(session))
    assert r.error_code is ErrorCode.CREATOR_NOT_FOUND


def test_username_found_but_no_result_fails_without_transient_retry():
    session = _session()
    session._page = make_result_page("yourboybigmike", {})  # username element but no ancestors
    r = _run(_extract(session))
    assert r.error_code is ErrorCode.CREATOR_NOT_FOUND


def test_row_without_dollar_and_no_detail_is_gmv_not_found():
    session = _session()
    session._page = make_result_page(
        "yourboybigmike", {2: "yourboybigmike", 3: "yourboybigmike\nM I K E"}
    )
    r = _run(_extract(session))
    assert r.error_code is ErrorCode.GMV_NOT_FOUND


def test_row_without_dollar_falls_back_to_detail_drawer():
    session = _session()
    session._page = make_result_page(
        "yourboybigmike", {2: "yourboybigmike", 3: "yourboybigmike\nM I K E"}, detail_gmv="$5K"
    )
    r = _run(_extract(session))
    assert r.status is RowStatus.SUCCESS
    assert r.source == "detail_panel"


def test_autocomplete_click_failure_advances_without_transient_retry():
    session = _session()
    session._page = make_suggestion_page("@yourboybigmike", click_bad=True)
    r = _run(_extract(session))
    assert r.error_code is ErrorCode.CREATOR_NOT_FOUND


def test_exact_visible_result_with_metrics_is_read_without_extra_click():
    session = _session()
    username = "yourboybigmike"
    exact = TextNode(username, {2: username, 3: ROW_FULL})
    page = Node()
    page.add_text(exact)
    session._page = page
    r = _run(_extract(session, username))
    assert exact.clicked is False
    assert r.status is RowStatus.SUCCESS


def test_unverified_top_profile_is_not_clicked_when_username_parsing_misses(monkeypatch):
    """An account-looking card without the exact handle is unsafe and must be skipped."""
    session = _session()
    session._page = Node()

    async def dropdown_without_parseable_handle(norm, timeout_ms=None):
        return [], True

    async def top_creator_profile(norm=None, *, suggestions=None):
        assert suggestions == []
        return None

    monkeypatch.setattr(session, "_wait_autocomplete", dropdown_without_parseable_handle)
    monkeypatch.setattr(session, "_top_creator_profile_suggestion", top_creator_profile)

    suggestion, info = _run(session._match_autocomplete("1_honest_creator"))
    assert suggestion is None
    assert info.saw_panel is True
    assert info.saw_suggestions is False
    assert info.used_top_profile is False


def test_creators_top_profile_card_has_priority_over_passive_exact_username(monkeypatch):
    """The screenshot's top card must win even when an exact but passive text node exists."""
    session = _session()
    passive_exact = Node("celiadailydeals")
    top_profile = Node("celiadailydeals\nceliadailydeals")
    session._page = Node()

    async def dropdown_with_exact_handle(norm, timeout_ms=None):
        return [passive_exact], True

    async def top_creator_profile_for(norm=None, *, suggestions=None):
        assert norm == "celiadailydeals"
        assert suggestions == [passive_exact]
        return top_profile

    monkeypatch.setattr(session, "_wait_autocomplete", dropdown_with_exact_handle)
    monkeypatch.setattr(session, "_top_creator_profile_suggestion", top_creator_profile_for)

    suggestion, info = _run(session._match_autocomplete("celiadailydeals"))
    assert suggestion is top_profile
    assert info.used_top_profile is True


def test_bare_creator_card_beats_at_prefixed_hashtag_result():
    """The creator account must win over the shorter ``@name`` hashtag entry."""
    session = _session()
    creator = Node("liubii\nLiubii")
    hashtag = Node("@liubii")
    session._page = Node()

    selected = _run(
        session._top_creator_profile_suggestion(
            "liubii",
            suggestions=[hashtag, creator],
        )
    )

    assert selected is creator


def test_at_prefixed_hashtag_alone_is_not_a_creator_suggestion():
    session = _session()
    hashtag = Node("@liubii")
    session._page = Node()

    selected = _run(
        session._top_creator_profile_suggestion("liubii", suggestions=[hashtag])
    )

    assert selected is None


def test_open_creators_dropdown_is_not_mistaken_for_activated_result(monkeypatch):
    """Seeing the handle inside the dropdown must keep activation false until its row opens."""
    session = _session()
    username = "celiadailydeals"
    exact = TextNode(username, {1: username, 2: username})
    page = Node()
    page.add_text(exact)
    session._page = page

    async def open_top_creator(norm=None):
        assert norm == username
        return Node(username)

    monkeypatch.setattr(session, "_top_creator_profile_suggestion", open_top_creator)
    assert _run(session._creator_activation_state(username)) == (False, False)


def test_creators_filter_tab_never_clicks_followers_for_missing_creator():
    """The page-level Creators/Followers filters are not an autocomplete account row."""
    session = _session()
    username = "lifewithlittleone"
    followers = Node("Followers")
    page = Node()
    page.add_text(Node("Creators"))
    page.add_text(followers)
    session._page = page

    result = _run(_extract(session, username))

    assert followers.clicked is False
    assert result.error_code is ErrorCode.CREATOR_NOT_FOUND


def test_stale_followers_state_is_restored_by_clicking_only_creators():
    session = _session()
    creators = Node("Creators")
    followers = Node("Followers")
    page = Node().add_text(creators).add_text(followers)
    session._page = page

    changed = _run(session._ensure_creators_filter_active())
    changed_again = _run(session._ensure_creators_filter_active())

    assert changed is True
    assert changed_again is False
    assert creators.clicked is True
    assert followers.clicked is False


def test_broad_filter_container_with_typed_handle_is_never_clicked():
    """The search wrapper can echo the handle but still owns the three filter tabs."""
    session = _session()
    username = "lifewithlittleone"
    unsafe = Node(f"{username}\nCreators\nFollowers\nPerformance")
    page = Node()
    page.add([selectors.render_autocomplete_suggestions(username)[0]], [unsafe])
    session._page = page

    result = _run(_extract(session, username))

    assert unsafe.clicked is False
    assert result.error_code is ErrorCode.CREATOR_NOT_FOUND


def test_autocomplete_username_click_resolves_to_enclosing_profile_card():
    """A passive username span should click its avatar-bearing option ancestor."""
    session = _session()
    card = Node("1_honest_creator")

    class PassiveUsername(Node):
        def locator(self, selector):
            if selector == "xpath=ancestor::*[@role='option'][1]":
                return _Loc([card])
            return _Loc([])

        async def click(self):
            raise RuntimeError("passive text is not clickable")

    _run(session._click_open(PassiveUsername("1_honest_creator"), ErrorCode.AUTOCOMPLETE_CLICK_FAILED))
    assert card.clicked is True


def test_prepare_search_input_clears_previous_value_before_new_creator():
    session = _session()

    class SearchInput(Node):
        def __init__(self):
            super().__init__("old_creator")
            self.actions = []

        async def press(self, key):
            self.actions.append(("press", key))

        async def fill(self, value):
            self.actions.append(("fill", value))
            self.text = value

    search = SearchInput()
    _run(session._prepare_search_input(search, "new_creator"))
    assert search.actions == [
        ("press", "Escape"),
        ("fill", ""),
        ("fill", "new_creator"),
    ]


def test_missing_autocomplete_submits_once_without_slow_retype(monkeypatch):
    """A missed dropdown goes straight to bounded submit instead of a second long wait."""
    session = _session()
    username = "third_creator"
    suggestion = TextNode(username, {2: username, 3: f"{username}\n$9.5K\n321"})
    page = Node()
    page.add_text(suggestion)
    session._page = page

    class SearchInput(Node):
        def __init__(self):
            super().__init__()
            self.typed = []

        async def press_sequentially(self, value, delay=0):
            self.typed.append((value, delay))
            self.text = value

    search = SearchInput()
    session._search_box = search
    attempts = 0

    async def delayed_match(norm, timeout_ms=None):
        nonlocal attempts
        attempts += 1
        info = type("Info", (), {
            "saw_panel": attempts > 1,
            "saw_suggestions": attempts > 1,
            "saw_username": attempts > 1,
        })()
        return (None, info) if attempts == 1 else (suggestion, info)

    monkeypatch.setattr(session, "_match_autocomplete", delayed_match)
    result = _run(_extract(session, username))

    assert attempts == 1
    assert search.typed == []
    assert result.status is RowStatus.SUCCESS
    assert result.gmv.value == 9500
    assert result.items_sold == 321


def test_search_wait_budget_prioritizes_exact_metrics_over_false_failure():
    # Successful rows return early, while a slow exact TikTok result gets a reliable upper bound.
    assert AUTOCOMPLETE_TIMEOUT_MS >= 1_000
    assert 4_000 <= SEARCH_ICON_RESULT_TIMEOUT_MS <= 6_000


def test_creator_emoji_is_safe_for_windows_console_logging():
    assert _console_safe("Honey ✨ creator", "cp949") == r"Honey \u2728 creator"


def test_changed_table_is_not_failed_before_late_exact_result(monkeypatch):
    """React can replace the table first and commit the exact row a few polls later."""
    session = _session()
    calls = 0

    async def activation_state(_norm):
        nonlocal calls
        calls += 1
        return (calls >= 4, calls >= 4)

    async def changed_signature():
        return "new table is rendering"

    monkeypatch.setattr(session, "_creator_activation_state", activation_state)
    monkeypatch.setattr(session, "_result_table_signature", changed_signature)

    found, has_money = _run(
        session._wait_for_submitted_result("late_creator", "previous table")
    )

    assert found is True
    assert has_money is True
    assert calls >= 4


def test_missing_suggestion_clicks_only_adjacent_search_icon(monkeypatch):
    """A missing creator uses the adjacent magnifier, never a page/filter control."""
    session = _session()

    class SearchIcon(Node):
        async def bounding_box(self):
            return {"x": 105, "y": 5, "width": 20, "height": 20}

    class SearchInput(Node):
        async def bounding_box(self):
            return {"x": 0, "y": 0, "width": 100, "height": 30}

        def locator(self, selector):
            if selector == SEARCH_ICON_RELATIVE_SELECTORS[0]:
                return _Loc([icon])
            return super().locator(selector)

    icon = SearchIcon()
    search = SearchInput()
    session._search_box = search
    session._page = Node()

    async def no_match(norm, timeout_ms=None):
        return None, type("Info", (), {
            "saw_panel": False,
            "saw_suggestions": False,
            "saw_username": False,
        })()

    monkeypatch.setattr(session, "_match_autocomplete", no_match)
    result = _run(_extract(session, "missing_creator"))

    assert icon.clicked is True
    assert result.error_code is ErrorCode.CREATOR_NOT_FOUND


def test_search_icon_extracts_gmv_and_items_when_result_renders(monkeypatch):
    """The live no-autocomplete build must extract metrics after its magnifier is clicked."""
    session = _session()
    username = "search_icon_creator"
    page = Node()
    session._page = page

    class SearchIcon(Node):
        async def bounding_box(self):
            return {"x": 105, "y": 5, "width": 20, "height": 20}

        async def click(self, timeout=None):
            self.clicked = True
            page.add_text(
                TextNode(
                    username,
                    {2: username, 3: f"{username}\n$27.4K\n812"},
                )
            )

    class SearchInput(Node):
        async def bounding_box(self):
            return {"x": 0, "y": 0, "width": 100, "height": 30}

        def locator(self, selector):
            if selector == SEARCH_ICON_RELATIVE_SELECTORS[0]:
                return _Loc([icon])
            return super().locator(selector)

    icon = SearchIcon()
    search = SearchInput()
    session._search_box = search

    async def no_match(norm, timeout_ms=None):
        return None, type("Info", (), {
            "saw_panel": False,
            "saw_suggestions": False,
            "saw_username": False,
        })()

    monkeypatch.setattr(session, "_match_autocomplete", no_match)
    result = _run(_extract(session, username))

    assert icon.clicked is True
    assert result.status is RowStatus.SUCCESS
    assert result.gmv.value == 27400
    assert result.items_sold == 812


def test_result_profile_is_clicked_automatically_when_row_has_no_money():
    session = _session()
    username = "needs_profile_click"
    exact = TextNode(username, {2: username, 3: f"{username}\nCreator profile"})
    page = Node()
    page.add_text(exact)
    panel = Node("detail", visible=True)
    panel.add([selectors.GMV_LABELS[1]], [Node("GMV")])
    panel.add([selectors.GMV_VALUES[0]], [Node("$12.5K")])
    page.add([selectors.DETAIL_PANEL[0]], [panel])
    session._page = page

    result = _run(_extract(session, username))

    assert exact.clicked is True
    assert result.status is RowStatus.SUCCESS
    assert result.gmv.value == 12500
    assert result.source == "detail_panel"


def test_autocomplete_similar_handle_is_not_matched():
    # Dropdown shows @arlynvibes2 while we search arlynvibes -> mismatch (no exact suggestion).
    session = _session()
    session._page = make_suggestion_page("@arlynvibes2")
    r = _run(_extract(session, "arlynvibes"))
    assert r.error_code is ErrorCode.CREATOR_NOT_FOUND


def test_cancel_during_autocomplete_wait_raises():
    session = _session()
    page = Node()
    page.add([selectors.AUTOCOMPLETE_PANEL[0]], [Node("Creators")])
    session._page = page
    event = asyncio.Event()
    event.set()
    session.cancel_event = event
    with pytest.raises(JobCancelledError):
        _run(_extract(session))


def test_transient_lookup_failure_is_retried_automatically(monkeypatch):
    """A missed result update must recover without asking the user to click the creator."""
    session = _session()
    session._page = FlowPage()
    attempts = 0
    recoveries = []

    async def once(requested):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return LookupResult(
                normalized_username=requested,
                status=RowStatus.FAILED,
                error_code=ErrorCode.RESULT_ROW_NOT_FOUND,
            )
        return LookupResult(normalized_username=requested, status=RowStatus.SUCCESS)

    async def recover(*, hard):
        recoveries.append(hard)

    monkeypatch.setattr(session, "_search_creator_once", once)
    monkeypatch.setattr(session, "_recover_lookup_state", recover)

    result = _run(session.search_creator("third_creator"))
    assert result.status is RowStatus.SUCCESS
    assert attempts == 2
    assert recoveries == [False]


def test_creator_not_found_fails_immediately_without_retry(monkeypatch):
    session = _session()
    session._page = FlowPage()
    attempts = 0

    async def once(requested):
        nonlocal attempts
        attempts += 1
        return LookupResult(
            normalized_username=requested,
            status=RowStatus.FAILED,
            error_code=ErrorCode.CREATOR_NOT_FOUND,
        )

    monkeypatch.setattr(session, "_search_creator_once", once)
    monkeypatch.setattr(session, "_recover_lookup_state", lambda **kwargs: asyncio.sleep(0))
    result = _run(session.search_creator("missing_creator"))
    assert result.error_code is ErrorCode.CREATOR_NOT_FOUND
    assert attempts == 1


def test_autocomplete_mismatch_fails_immediately_without_outer_retry(monkeypatch):
    session = _session()
    session._page = FlowPage()
    attempts = 0

    async def once(requested):
        nonlocal attempts
        attempts += 1
        return LookupResult(
            normalized_username=requested,
            status=RowStatus.FAILED,
            error_code=ErrorCode.AUTOCOMPLETE_MISMATCH,
        )

    monkeypatch.setattr(session, "_search_creator_once", once)
    monkeypatch.setattr(session, "_recover_lookup_state", lambda **kwargs: asyncio.sleep(0))
    result = _run(session.search_creator("missing_creator"))
    assert result.error_code is ErrorCode.AUTOCOMPLETE_MISMATCH
    assert attempts == 1


def test_third_attempt_uses_hard_workspace_recovery(monkeypatch):
    session = _session()
    session._page = FlowPage()
    attempts = 0
    recoveries = []

    async def once(requested):
        nonlocal attempts
        attempts += 1
        return LookupResult(
            normalized_username=requested,
            status=RowStatus.FAILED,
            error_code=ErrorCode.AUTOCOMPLETE_CLICK_FAILED,
        )

    async def recover(*, hard):
        recoveries.append(hard)

    monkeypatch.setattr(session, "_search_creator_once", once)
    monkeypatch.setattr(session, "_recover_lookup_state", recover)
    _run(session.search_creator("stubborn_creator"))

    assert attempts == 3
    assert recoveries == [False, True]

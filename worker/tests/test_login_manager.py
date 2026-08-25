"""Login/verify URL-separation tests (Seller Center login vs Affiliate Center lookup).

The real Playwright DOM is UNVERIFIED (D10); a minimal fake ``playwright`` module is injected
so the *navigation targets* and the conservative login detection can be asserted without a
real browser. The fake page has nothing visible, so it stands in for a blank/loading screen —
which must never be reported as ``connected``.
"""

import asyncio
import sys
import types

import pytest

from gmv import login_manager
from gmv.automation.session import TikTokAffiliateSession
from gmv.config import ProfileStatus, get_default_profile


class _FakeLoc:
    def __init__(self, visible: bool = False, text: str = ""):
        self._visible = visible
        self._text = text

    @property
    def first(self):
        return self

    async def wait_for(self, state: str = "visible", timeout: int = 0):
        if not self._visible:
            raise TimeoutError("not visible")

    async def inner_text(self, timeout: int = 0):
        return self._text

    async def all(self):
        return [self] if self._visible else []

    async def count(self):
        return 1 if self._visible else 0

    async def is_visible(self):
        return self._visible

    async def click(self, timeout: int = 0):
        return None


class _FakePage:
    """A blank page: no element is visible, so no login can be positively confirmed."""

    def __init__(self, urls: list, url: str = "about:blank"):
        self._urls = urls
        self.url = url

    async def goto(self, url: str, wait_until: str | None = None):
        self._urls.append(url)
        self.url = url

    def on(self, *args, **kwargs):
        return None

    def locator(self, selector: str):
        return _FakeLoc(visible=False)


class _FakeContext:
    def __init__(self, urls: list):
        self.pages: list = []
        self._urls = urls

    async def new_page(self):
        page = _FakePage(self._urls)
        self.pages.append(page)
        return page

    async def close(self):
        return None


class _FakeChromium:
    def __init__(self, urls: list):
        self._urls = urls

    async def launch_persistent_context(self, **kwargs):
        return _FakeContext(self._urls)


class _FakePW:
    def __init__(self, urls: list):
        self.chromium = _FakeChromium(urls)

    async def stop(self):
        return None


class _FakeStarter:
    def __init__(self, urls: list):
        self._urls = urls

    async def start(self):
        return _FakePW(self._urls)


@pytest.fixture
def fake_pw(monkeypatch, tmp_path):
    """Inject a fake playwright module and isolate status files under tmp."""
    monkeypatch.setenv("GMV_STORAGE_ROOT", str(tmp_path / "jobs"))
    urls: list = []

    def factory():
        return _FakeStarter(urls)

    mod = types.ModuleType("playwright")
    sub = types.ModuleType("playwright.async_api")
    sub.async_playwright = factory
    mod.async_api = sub
    monkeypatch.setitem(sys.modules, "playwright", mod)
    monkeypatch.setitem(sys.modules, "playwright.async_api", sub)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    return urls


def test_open_login_opens_login_url_not_affiliate(fake_pw, monkeypatch):
    monkeypatch.setattr(login_manager, "LOGIN_POLL_SECONDS", 1)
    profile = get_default_profile()
    result = asyncio.run(login_manager.open_login("DEFAULT"))

    assert profile.login_url in fake_pw
    assert profile.affiliate_url not in fake_pw
    # A blank Seller Center page must never be reported as connected.
    assert result["status"] != ProfileStatus.CONNECTED.value
    assert result["status"] == ProfileStatus.LOGIN_REQUIRED.value


class _AuthenticatedUsPage(_FakePage):
    """Seller login succeeds and its US Affiliate hand-off remains authenticated."""

    async def goto(self, url: str, wait_until: str | None = None):
        self._urls.append(url)
        profile = get_default_profile()
        self.url = "https://seller-us.tiktok.com/dashboard" if url == profile.login_url else url

    async def wait_for_url(self, pattern: str, timeout: int = 0):
        raise TimeoutError("fake Seller page does not auto-redirect")

    def is_closed(self):
        return False

    def locator(self, selector: str):
        from gmv.automation import selectors

        profile = get_default_profile()
        return _FakeLoc(
            visible=self.url == profile.find_creators_url and selector in selectors.SEARCH_INPUT
        )


def test_us_login_defers_creator_search_until_gmv_job(monkeypatch, tmp_path):
    urls = _install_fake_pw(monkeypatch, tmp_path, _AuthenticatedUsPage)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    result = asyncio.run(login_manager.open_login("US_CHROME"))
    profile = get_default_profile()

    assert result["status"] == ProfileStatus.CONNECTED.value
    assert urls == [profile.login_url]
    assert profile.login_success_url not in urls
    assert profile.find_creators_url not in urls
    assert profile.affiliate_entry_url not in urls


class _DynamicChallengeLoc(_FakeLoc):
    """A challenge locator whose visibility is controlled by its owning page."""

    def __init__(self, page):
        super().__init__(visible=True)
        self._page = page

    async def count(self):
        return 1

    async def is_visible(self):
        return self._page.challenge_is_visible()


class _PuzzleOnSellerHandoffPage(_AuthenticatedUsPage):
    """The US puzzle appears on Seller hand-off, then disappears after manual completion."""

    def __init__(self, urls: list, url: str = "about:blank"):
        super().__init__(urls, url)
        self._challenge_polls_left = 2

    def challenge_is_visible(self):
        if self._challenge_polls_left > 0:
            self._challenge_polls_left -= 1
            return True
        return False

    def locator(self, selector: str):
        from gmv.automation.session import SECURITY_CHALLENGE_SELECTORS

        if selector == SECURITY_CHALLENGE_SELECTORS[0]:
            return _DynamicChallengeLoc(self)
        return _FakeLoc(visible=False)


def test_us_login_does_not_trigger_handoff_puzzle(monkeypatch, tmp_path):
    urls = _install_fake_pw(monkeypatch, tmp_path, _PuzzleOnSellerHandoffPage)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    result = asyncio.run(login_manager.open_login("US_CHROME"))
    profile = get_default_profile()

    assert result["status"] == ProfileStatus.CONNECTED.value
    assert urls == [profile.login_url]
    assert profile.login_success_url not in urls
    assert profile.find_creators_url not in urls


class _RetryLoc(_FakeLoc):
    def __init__(self, on_click):
        super().__init__(visible=True)
        self._on_click = on_click

    async def click(self, timeout: int = 0):
        self._on_click()


class _ErrorBeforePuzzleThenRetryPage(_AuthenticatedUsPage):
    """TikTok errors before the puzzle, then one Retry restores the Seller hand-off."""

    def __init__(self, urls: list, url: str = "about:blank"):
        super().__init__(urls, url)
        self.handoff_state = "initial"

    async def goto(self, url: str, wait_until: str | None = None):
        profile = get_default_profile()
        if url == profile.login_success_url and self.handoff_state == "initial":
            self._urls.append(url)
            self.url = "https://seller-us.tiktok.com/errorpage"
            self.handoff_state = "error"
            return
        await super().goto(url, wait_until)

    def locator(self, selector: str):
        if self.handoff_state == "error" and "Retry" in selector:
            return _RetryLoc(self._retry)
        return _FakeLoc(visible=False)

    def _retry(self):
        self.handoff_state = "ready"
        self.url = get_default_profile().login_success_url


def test_us_login_retries_once_before_puzzle_without_opening_creator(monkeypatch, tmp_path):
    urls = _install_fake_pw(monkeypatch, tmp_path, _ErrorBeforePuzzleThenRetryPage)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    result = asyncio.run(login_manager.open_login("US_CHROME"))
    profile = get_default_profile()

    assert result["status"] == ProfileStatus.CONNECTED.value
    assert result["last_error"] is None
    assert urls == [profile.login_url]
    assert profile.login_success_url not in urls
    assert profile.find_creators_url not in urls


class _SearchBehindPuzzlePage(_AuthenticatedUsPage):
    """The search field exists in the DOM but must not count while the puzzle is visible."""

    def locator(self, selector: str):
        from gmv.automation import selectors
        from gmv.automation.session import SECURITY_CHALLENGE_SELECTORS

        if selector in SECURITY_CHALLENGE_SELECTORS:
            return _FakeLoc(visible=True)
        return _FakeLoc(visible=selector in selectors.SEARCH_INPUT)


def test_us_login_does_not_connect_to_search_hidden_behind_puzzle(monkeypatch, tmp_path):
    urls = _install_fake_pw(monkeypatch, tmp_path, _SearchBehindPuzzlePage)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    result = asyncio.run(login_manager.open_login("US_CHROME"))

    assert result["status"] == ProfileStatus.CONNECTED.value
    assert urls == [get_default_profile().login_url]
    assert get_default_profile().login_success_url not in urls
    assert get_default_profile().find_creators_url not in urls


def _install_fake_pw(monkeypatch, tmp_path, page_factory) -> list:
    """Install a fake playwright whose new page is built by ``page_factory(urls)``."""
    monkeypatch.setenv("GMV_STORAGE_ROOT", str(tmp_path / "jobs"))
    urls: list = []

    class _Ctx(_FakeContext):
        async def new_page(self):
            page = page_factory(self._urls)
            self.pages.append(page)
            return page

    class _Chromium(_FakeChromium):
        async def launch_persistent_context(self, **kwargs):
            return _Ctx(self._urls)

    class _PW(_FakePW):
        def __init__(self, u):
            self.chromium = _Chromium(u)

    class _Starter(_FakeStarter):
        async def start(self):
            return _PW(self._urls)

    mod = types.ModuleType("playwright")
    sub = types.ModuleType("playwright.async_api")
    sub.async_playwright = lambda: _Starter(urls)
    mod.async_api = sub
    monkeypatch.setitem(sys.modules, "playwright", mod)
    monkeypatch.setitem(sys.modules, "playwright.async_api", sub)
    return urls


class _RedirectToRegisterPage(_FakePage):
    """A logged-out session: any goto bounces to the Seller login/register page."""

    async def goto(self, url, wait_until=None):
        self._urls.append(url)
        self.url = "https://seller-us.tiktok.com/account/register"


def test_verify_uses_login_success_url_and_does_not_falsely_connect(monkeypatch, tmp_path):
    # verify checks the login-success page; a logged-out redirect must NOT be connected.
    urls = _install_fake_pw(monkeypatch, tmp_path, _RedirectToRegisterPage)
    profile = get_default_profile()
    result = asyncio.run(login_manager.verify("DEFAULT"))

    assert profile.login_success_url in urls
    assert result["status"] != ProfileStatus.CONNECTED.value
    assert result["status"] == ProfileStatus.EXPIRED.value


def test_gmv_session_opens_creator_without_obsolete_target_invitation(fake_pw):
    # Seller landing establishes the selected shop; target-invitation is intentionally skipped.
    profile = get_default_profile()
    session = TikTokAffiliateSession(profile, headless=True)
    asyncio.run(session.start())

    assert profile.find_creators_url in fake_pw
    assert profile.affiliate_entry_url not in fake_pw
    assert profile.login_url not in fake_pw
    assert profile.login_success_url in fake_pw
    assert fake_pw.index(profile.login_success_url) < fake_pw.index(profile.find_creators_url)


def test_account_register_url_is_not_treated_as_logged_out():
    # /account/register is the normal Seller Center login entry, not an expired session (§4,§5).
    from gmv.automation import selectors

    assert selectors.is_logged_out_url("https://seller-us.tiktok.com/account/register") is False


def test_detect_seller_login_on_register_page_is_login_required_not_error(fake_pw):
    # A blank/login page at /account/register must yield login_required, never connected/error.
    page = _FakePage([], url="https://seller-us.tiktok.com/account/register")
    status = asyncio.run(login_manager.detect_seller_login(page))
    assert status is ProfileStatus.LOGIN_REQUIRED


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://seller-us.tiktok.com/affiliate/landing?is_new_connect=0&shop_region=US", True),
        ("https://seller-us.tiktok.com/affiliate/landing", True),
        ("https://seller-us.tiktok.com/affiliate/landing?foo=bar&x=1", True),  # query may differ
        ("https://seller-uk.tiktok.com/affiliate/landing?shop_region=GB", True),
        ("https://seller-us.tiktok.com/account/register", False),
        ("https://accounts.tiktok.com/login", False),
        ("about:blank", False),
        ("", False),
    ],
)
def test_is_seller_login_success(url, expected):
    assert login_manager.is_seller_login_success(url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://seller-us.tiktok.com/homepage?shop_region=US", True),
        ("https://seller-us.tiktok.com/dashboard", True),
        ("https://seller-us.tiktok.com/affiliate/landing", True),
        ("https://seller-us.tiktok.com/account/register", False),
        ("https://seller-us.tiktok.com/", False),
        ("https://accounts.tiktok.com/login", False),
        ("about:blank", False),
    ],
)
def test_is_authenticated_seller_url(url, expected):
    assert (
        login_manager.is_authenticated_seller_url(url, "seller-us.tiktok.com") is expected
    )


class _DashboardPage(_AuthenticatedUsPage):
    """A successful login that lands on Seller Center home instead of affiliate/landing."""


def test_open_login_connects_on_authenticated_seller_home(monkeypatch, tmp_path):
    _install_fake_pw(monkeypatch, tmp_path, _DashboardPage)
    monkeypatch.setattr(login_manager, "LOGIN_POLL_SECONDS", 1)

    result = asyncio.run(login_manager.open_login("DEFAULT"))

    assert result["status"] == ProfileStatus.CONNECTED.value


class _LandingPage(_FakePage):
    """After any goto, the session keeps us on the login-success landing page."""

    async def goto(self, url, wait_until=None):
        self._urls.append(url)
        self.url = get_default_profile().login_success_url


def test_verify_connected_when_landing_and_no_login_form(monkeypatch, tmp_path):
    # verify() must return connected when the success page persists and no login form shows.
    _install_fake_pw(monkeypatch, tmp_path, _LandingPage)
    result = asyncio.run(login_manager.verify("DEFAULT"))
    assert result["status"] == ProfileStatus.CONNECTED.value

"""Safe Playwright workflow for accepting exact Target Invitations."""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from gmv.automation import selectors
from gmv.config import BrowserProfile, ProfileStatus
from gmv.invitation_acceptor import InvitationSpec, is_exact_invitation_match
from gmv.models import JobCancelledError

MAX_SEARCH_PAGES = 100
TARGET_PATH = "/affiliate/collaboration/target-invitation"


class InvitationAcceptError(RuntimeError):
    error_code = "UNKNOWN_ERROR"


class LoginRequiredError(InvitationAcceptError):
    error_code = "LOGIN_REQUIRED"


class MarketMismatchError(InvitationAcceptError):
    error_code = "MARKET_MISMATCH"


class SearchFailedError(InvitationAcceptError):
    error_code = "SEARCH_FAILED"


class InvitationNotFoundError(InvitationAcceptError):
    error_code = "NOT_FOUND"


class DetailFailedError(InvitationAcceptError):
    error_code = "DETAIL_FAILED"


class AcceptButtonNotFoundError(InvitationAcceptError):
    error_code = "ACCEPT_BUTTON_NOT_FOUND"


class ConfirmFailedError(InvitationAcceptError):
    error_code = "CONFIRM_FAILED"


class AcceptVerifyFailedError(InvitationAcceptError):
    error_code = "ACCEPT_VERIFY_FAILED"


@dataclass(frozen=True)
class AcceptOutcome:
    status: str
    message: str


def _origin(url: str) -> str:
    parsed = urlparse(str(url or ""))
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


def target_invitation_url(profile: BrowserProfile, current_url: str = "") -> str:
    """Use the confirmed US origin and derive UK from the authenticated Affiliate origin."""
    if profile.market.upper() == "US":
        return f"https://affiliate-us.tiktok.com{TARGET_PATH}"
    current_origin = _origin(current_url)
    configured_origin = _origin(profile.affiliate_entry_url)
    if "affiliate" in urlparse(current_origin).netloc and "affiliate-us" not in current_origin:
        return f"{current_origin}{TARGET_PATH}"
    if "affiliate" in urlparse(configured_origin).netloc and "affiliate-us" not in configured_origin:
        return f"{configured_origin}{TARGET_PATH}"
    raise MarketMismatchError("로그인된 UK TikTok Affiliate origin을 확인할 수 없습니다.")


def inspect_json_for_invitation_names(value) -> set[str]:
    """Extract only explicit invitation-name fields from observed page JSON responses."""
    names: set[str] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                folded = str(key).replace("_", "").casefold()
                if folded in {"invitationname", "targetinvitationname"} and isinstance(child, str):
                    text = child.strip()
                    if text:
                        names.add(text)
                elif isinstance(child, (dict, list)):
                    stack.append(child)
        elif isinstance(item, list):
            stack.extend(item)
    return names


class TikTokInvitationAcceptorSession:
    """One Job owns one persistent context and exactly one page/window."""

    def __init__(self, profile: BrowserProfile, *, headless: bool = False, cancel_event=None):
        self.profile = profile
        self.headless = headless
        self.cancel_event = cancel_event
        self._pw = None
        self._context = None
        self._page = None
        self._target_url = ""
        self._network_names: set[str] = set()
        self._positive_accept_response = False
        self._pending_accept_name = ""

    def _check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise JobCancelledError

    async def start(self) -> ProfileStatus:
        from playwright.async_api import async_playwright

        self._check_cancel()
        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=self.profile.storage_root,
            channel=self.profile.browser_channel,
            headless=self.headless,
            chromium_sandbox=True,
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._page.on("response", lambda response: asyncio.create_task(self._capture_response(response)))
        await self._page.goto(self.profile.login_success_url, wait_until="domcontentloaded")
        if selectors.is_affiliate_login_redirect(self._page.url):
            return ProfileStatus.LOGIN_REQUIRED
        # UK Affiliate hosts may vary. Enter through the configured authenticated hand-off,
        # then derive the Target Invitation origin from the URL TikTok actually selected.
        if self.profile.market.upper() == "UK":
            await self._page.goto(self.profile.affiliate_entry_url, wait_until="domcontentloaded")
            if selectors.is_affiliate_login_redirect(self._page.url):
                return ProfileStatus.LOGIN_REQUIRED
        self._target_url = target_invitation_url(self.profile, self._page.url)
        await self._page.goto(self._target_url, wait_until="domcontentloaded")
        await self._page.wait_for_timeout(400)
        if selectors.is_affiliate_login_redirect(self._page.url):
            return ProfileStatus.LOGIN_REQUIRED
        self._verify_market()
        return ProfileStatus.CONNECTED

    async def close(self) -> None:
        if self._context is not None:
            with contextlib.suppress(Exception):
                await self._context.close()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
        self._context = None
        self._pw = None
        self._page = None

    async def _capture_response(self, response) -> None:
        with contextlib.suppress(Exception):
            content_type = (await response.header_value("content-type") or "").casefold()
            if "json" not in content_type:
                return
            payload = await response.json()
            self._network_names.update(inspect_json_for_invitation_names(payload))
            url = response.url.casefold()
            request = response.request
            method = request.method.upper()
            request_body = (request.post_data or "").casefold()
            pending = self._pending_accept_name.casefold()
            response_names = inspect_json_for_invitation_names(payload)
            target_linked = bool(pending) and (
                pending in request_body
                or any(is_exact_invitation_match(name, self._pending_accept_name) for name in response_names)
            )
            if (
                response.ok
                and method in {"POST", "PUT", "PATCH"}
                and any(word in url for word in ("accept", "join"))
                and target_linked
            ):
                text = str(payload).casefold()
                if any(word in text for word in ("success", "accepted", "joined")):
                    self._positive_accept_response = True

    def _verify_market(self) -> None:
        host = urlparse(self._page.url).netloc.casefold()
        if self.profile.market.upper() == "US" and host != "affiliate-us.tiktok.com":
            raise MarketMismatchError(f"US 작업이 다른 Market 페이지에서 열렸습니다: {host}")
        if self.profile.market.upper() == "UK" and ("affiliate" not in host or host == "affiliate-us.tiktok.com"):
            raise MarketMismatchError(f"UK 작업이 다른 Market 페이지에서 열렸습니다: {host}")

    async def accept_invitation(self, spec: InvitationSpec, progress_cb=None) -> AcceptOutcome:
        self._check_cancel()
        await self._ensure_target_page()
        await self._emit(progress_cb, "search", f"{spec.product} 검색 시작", 0, 0)
        exact_node = await self._find_exact(spec, progress_cb)
        await self._emit(progress_cb, "detail", f"{spec.full_name} 발견 · 상세 페이지 이동")
        scope = await self._open_exact_candidate(exact_node, spec.full_name)
        await self._verify_detail_identity(scope, spec.full_name)

        if await self._has_accepted_status(scope):
            await self._close_detail()
            return AcceptOutcome("ALREADY_ACCEPTED", "이미 수락된 초대장입니다.")

        accept_button = await self._accept_button(scope)
        if accept_button is None:
            raise AcceptButtonNotFoundError("정확히 일치한 초대장 상세에서 Accept 버튼을 찾지 못했습니다.")
        self._positive_accept_response = False
        self._pending_accept_name = spec.full_name
        await self._emit(progress_cb, "accept", "Exact Match 재검증 완료 · 수락 요청")
        try:
            await accept_button.click()
            await self._confirm_if_present(spec.full_name)
            if not await self._verify_accepted(scope, accept_button):
                raise AcceptVerifyFailedError("Accept 후 수락 완료 상태를 확인하지 못했습니다.")
        finally:
            self._pending_accept_name = ""
        await self._close_detail()
        return AcceptOutcome("SUCCESS", "수락 완료 상태를 확인했습니다.")

    async def _ensure_target_page(self) -> None:
        self._check_cancel()
        if selectors.is_affiliate_login_redirect(self._page.url):
            raise LoginRequiredError("TikTok 로그인이 만료되었습니다.")
        if TARGET_PATH not in urlparse(self._page.url).path:
            await self._page.goto(self._target_url, wait_until="domcontentloaded")
            await self._page.wait_for_timeout(300)
        self._verify_market()

    async def _search_input(self):
        locator = self._page.locator(
            'input[placeholder*="Invitation" i], input[placeholder*="Search" i], '
            'input[placeholder*="초대"], input[aria-label*="Invitation" i], '
            'input[aria-label*="Search" i]'
        )
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
        raise SearchFailedError("Target Invitation 검색창을 찾지 못했습니다.")

    async def _find_exact(self, spec: InvitationSpec, progress_cb):
        search = await self._search_input()
        await search.fill(spec.product)
        await search.press("Enter")
        await self._page.wait_for_timeout(500)
        visited: set[str] = set()
        for page_number in range(1, MAX_SEARCH_PAGES + 1):
            self._check_cancel()
            await self._emit(progress_cb, "search", f"Search Page {page_number} 확인", page_number, 0)
            exact = self._page.get_by_text(spec.full_name, exact=True)
            for index in range(await exact.count()):
                node = exact.nth(index)
                if not await node.is_visible():
                    continue
                actual = (await node.inner_text()).strip()
                # Product/date/number checks narrow the candidate; this exact comparison is the
                # non-bypassable safety gate immediately before opening and accepting it.
                if (
                    spec.product.casefold() in actual.casefold()
                    and spec.date.casefold() in actual.casefold()
                    and re.search(rf"_{re.escape(spec.number)}$", actual)
                    and is_exact_invitation_match(actual, spec.full_name)
                ):
                    return node
            # Observed JSON can prove absence/presence but never authorizes a click by itself.
            if any(is_exact_invitation_match(name, spec.full_name) for name in self._network_names):
                await self._emit(progress_cb, "search", "API 응답에서 후보 확인 · DOM exact match 대기")
            signature = (await self._page.locator("body").inner_text())[-3000:]
            if signature in visited:
                break
            visited.add(signature)
            if not await self._advance_next():
                break
        raise InvitationNotFoundError(f"모든 검색 페이지에서 정확한 초대장을 찾지 못했습니다: {spec.full_name}")

    async def _open_exact_candidate(self, exact_node, requested: str):
        actual = (await exact_node.inner_text()).strip()
        if not is_exact_invitation_match(actual, requested):
            raise DetailFailedError("검색 결과의 초대장명이 요청값과 정확히 일치하지 않습니다.")
        row = exact_node.locator(
            "xpath=ancestor::*[self::tr or @role='row' or contains(@class,'card')][1]"
        )
        if await row.count():
            view = row.get_by_role("button", name=re.compile(r"View|Details|보기|상세", re.I))
            if await view.count() and await view.first.is_visible():
                await view.first.click()
            else:
                await exact_node.click()
        else:
            await exact_node.click()
        await self._page.wait_for_timeout(350)
        return await self._detail_scope()

    async def _detail_scope(self):
        scopes = self._page.locator(
            '[role="dialog"]:visible, [class*="drawer" i]:visible, [class*="modal" i]:visible, '
            '[class*="detail" i]:visible'
        )
        return scopes.last if await scopes.count() else self._page

    async def _verify_detail_identity(self, scope, requested: str) -> None:
        exact = scope.get_by_text(requested, exact=True)
        for index in range(await exact.count()):
            node = exact.nth(index)
            if await node.is_visible() and is_exact_invitation_match(await node.inner_text(), requested):
                return
        raise DetailFailedError("상세 영역에서 Full Invitation Name exact match를 재검증하지 못했습니다.")

    async def _has_accepted_status(self, scope) -> bool:
        status = scope.get_by_text(
            re.compile(r"^\s*(Accepted|Joined|Completed|수락 완료|수락됨|참여 완료)\s*$", re.I)
        )
        return await self._any_visible(status)

    async def _accept_button(self, scope):
        buttons = scope.get_by_role(
            "button", name=re.compile(r"^\s*(Accept|Accept invitation|Join|수락|참여)\s*$", re.I)
        )
        for index in range(await buttons.count()):
            button = buttons.nth(index)
            if await button.is_visible() and not await button.is_disabled():
                return button
        return None

    async def _confirm_if_present(self, requested: str) -> None:
        await self._page.wait_for_timeout(150)
        dialogs = self._page.locator('[role="dialog"]:visible, [class*="modal" i]:visible')
        if await dialogs.count() == 0:
            return
        dialog = dialogs.last
        text = (await dialog.inner_text()).casefold()
        if requested.casefold() not in text and not any(word in text for word in ("accept", "join", "수락", "confirm")):
            raise ConfirmFailedError("수락 확인 Modal의 대상을 검증하지 못했습니다.")
        confirm = dialog.get_by_role(
            "button", name=re.compile(r"^\s*(Confirm|Accept|Accept invitation|Join|확인|수락)\s*$", re.I)
        )
        for index in range(await confirm.count()):
            button = confirm.nth(index)
            if await button.is_visible() and not await button.is_disabled():
                await button.click()
                return
        raise ConfirmFailedError("수락 확인 Modal에서 Confirm 버튼을 찾지 못했습니다.")

    async def _verify_accepted(self, scope, accept_button) -> bool:
        for _ in range(40):
            self._check_cancel()
            if self._positive_accept_response or await self._has_accepted_status(scope):
                return True
            with contextlib.suppress(Exception):
                if not await accept_button.is_visible():
                    return True
            toast = self._page.get_by_text(re.compile(r"success|accepted|joined|수락.*완료", re.I))
            if await self._any_visible(toast):
                return True
            await self._page.wait_for_timeout(250)
        return False

    async def _advance_next(self) -> bool:
        candidates = self._page.locator(
            'button[aria-label*="next" i], li[title*="next" i] button, '
            '[class*="pagination-next" i] button'
        )
        if await candidates.count() == 0:
            candidates = self._page.get_by_role("button", name=re.compile(r"^(Next|다음|>)$", re.I))
        for index in range(await candidates.count()):
            button = candidates.nth(index)
            if not await button.is_visible():
                continue
            if await button.is_disabled() or (await button.get_attribute("aria-disabled")) == "true":
                continue
            await button.click()
            await self._page.wait_for_timeout(300)
            return True
        return False

    async def _close_detail(self) -> None:
        close = self._page.locator(
            '[role="dialog"]:visible button[aria-label*="close" i], '
            '[class*="drawer" i]:visible button[aria-label*="close" i], '
            '[role="dialog"]:visible button:has-text("Close")'
        )
        if await close.count():
            with contextlib.suppress(Exception):
                await close.first.click()
                await self._page.wait_for_timeout(200)
                return
        if TARGET_PATH not in urlparse(self._page.url).path:
            with contextlib.suppress(Exception):
                await self._page.go_back(wait_until="domcontentloaded")

    async def _emit(self, callback, phase: str, message: str, page: int = 0, total: int = 0) -> None:
        if callback is None:
            return
        result = callback(phase, message, page, total)
        if asyncio.iscoroutine(result):
            await result

    async def _any_visible(self, locator) -> bool:
        for index in range(await locator.count()):
            if await locator.nth(index).is_visible():
                return True
        return False

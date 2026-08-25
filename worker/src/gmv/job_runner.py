"""Job orchestration for the single-profile MVP workflow."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from gmv.config import (
    BrowserProfile,
    ProfileStatus,
    get_default_profile,
    get_max_concurrency,
    get_profile,
)
from gmv.excel_engine import WorkbookPlan, read_workbook, write_results
from gmv.models import ErrorCode, JobCancelledError, LookupResult, RowStatus

# A session must implement: async start()->ProfileStatus, async search_creator(str)->LookupResult,
# async close()->None.
SessionFactory = Callable[[BrowserProfile], object]
ProgressCallback = Callable[["JobProgress"], Awaitable[None] | None]

# A Playwright operation can occasionally remain pending even though all of its own selector
# timeouts are bounded. The outer watchdog is deliberately independent of the DOM implementation:
# it can discard the whole browser session and retry the same creator from a clean page.
LOOKUP_WATCHDOG_SECONDS = max(15.0, float(os.environ.get("GMV_WATCHDOG_SECONDS", "45")))
SESSION_START_WATCHDOG_SECONDS = max(
    LOOKUP_WATCHDOG_SECONDS,
    float(os.environ.get("GMV_SESSION_START_SECONDS", "75")),
)
SESSION_CLOSE_TIMEOUT_SECONDS = max(
    2.0, float(os.environ.get("GMV_SESSION_CLOSE_SECONDS", "10"))
)
MAX_SESSION_RESTARTS_PER_CREATOR = max(
    0, int(os.environ.get("GMV_SESSION_RESTARTS", "2"))
)
WATCHDOG_POLL_SECONDS = 0.5
HEARTBEAT_SECONDS = 5.0

_SESSION_RECOVERY_ERRORS = {
    "AUTOCOMPLETE_CLICK_FAILED",
    "BROWSER_ERROR",
    "CREATOR_OPEN_FAILED",
    "RESULT_ROW_NOT_FOUND",
    "SEARCH_PAGE_NOT_FOUND",
    "SELECTOR_ERROR",
}


@dataclass
class JobProgress:
    total: int
    processed: int = 0
    success: int = 0
    range: int = 0
    failed: int = 0
    current: str | None = None
    # Persisted by the service after each creator so a long workbook survives a Worker restart.
    last_username: str | None = None
    last_result: LookupResult | None = None


@dataclass
class JobResult:
    plan: WorkbookPlan
    results: dict[str, LookupResult] = field(default_factory=dict)
    output_path: str | None = None
    cancelled: bool = False

    @property
    def progress(self) -> JobProgress:
        p = JobProgress(total=len(self.results))
        for r in self.results.values():
            p.processed += 1
            if r.status is RowStatus.SUCCESS:
                p.success += 1
            elif r.status is RowStatus.RANGE:
                p.range += 1
            elif r.status is RowStatus.FAILED:
                p.failed += 1
        return p


def _assign_profiles(
    plan: WorkbookPlan, selected_profile_code: str | None
) -> dict[str, list[str]]:
    """Group every unique username under a single default profile."""
    groups: dict[str, dict[str, None]] = {}
    for sheet in plan.sheets:
        for row in sheet.rows:
            if not row.normalized_username:
                continue
            code = selected_profile_code or get_default_profile().profile_code
            groups.setdefault(code, {}).setdefault(row.normalized_username, None)
    return {code: list(names.keys()) for code, names in groups.items()}


async def _emit(cb: ProgressCallback | None, progress: JobProgress) -> None:
    if cb is None:
        return
    out = cb(progress)
    if asyncio.iscoroutine(out):
        await out


def _error_code_name(result: LookupResult) -> str:
    code = getattr(result, "error_code", None)
    return str(getattr(code, "value", code) or "").upper()


def _login_status(status: ProfileStatus) -> bool:
    return status in {ProfileStatus.EXPIRED, ProfileStatus.LOGIN_REQUIRED}


def _status_error_code(status: ProfileStatus) -> ErrorCode:
    if status is ProfileStatus.EXPIRED:
        return ErrorCode.SESSION_EXPIRED
    if status is ProfileStatus.LOGIN_REQUIRED:
        return ErrorCode.LOGIN_REQUIRED
    return ErrorCode.BROWSER_ERROR


def _failed_lookup(profile: BrowserProfile, name: str, code: ErrorCode, message: str) -> LookupResult:
    return LookupResult(
        normalized_username=name,
        status=RowStatus.FAILED,
        error_code=code,
        error_message=message[:300],
        account_label=profile.profile_code,
    )


async def _await_with_watchdog(
    awaitable,
    *,
    session,
    timeout_seconds: float,
    heartbeat: Callable[[], Awaitable[None]] | None = None,
):
    """Await one browser operation while enforcing real no-progress recovery.

    A visible TikTok security puzzle pauses the deadline because the user must solve it manually.
    No other page/URL/input state can extend the deadline. Cancellation is bounded as well, so a
    wedged Playwright task cannot prevent the browser-restart path from running.
    """
    task = asyncio.create_task(awaitable)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    next_heartbeat = loop.time() + HEARTBEAT_SECONDS
    puzzle_was_active = False

    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=WATCHDOG_POLL_SECONDS)
            if done:
                return await task

            now = loop.time()
            if getattr(session, "security_challenge_active", False):
                # The only manual step. Keep moving the no-progress deadline while the puzzle is
                # visibly present; the session's own three-minute verification limit still applies.
                puzzle_was_active = True
                deadline = now + timeout_seconds
            elif puzzle_was_active:
                # Start a fresh normal deadline when the user finishes the puzzle. The browser
                # still needs a short render window after the verification overlay disappears.
                puzzle_was_active = False
                deadline = now + timeout_seconds
            elif now >= deadline:
                raise TimeoutError(f"browser made no progress for {timeout_seconds:.0f}s")

            if heartbeat is not None and now >= next_heartbeat:
                await heartbeat()
                next_heartbeat = now + HEARTBEAT_SECONDS
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
                await asyncio.wait_for(task, timeout=2.0)


async def _close_session(session) -> None:
    if session is None:
        return
    with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
        await asyncio.wait_for(session.close(), timeout=SESSION_CLOSE_TIMEOUT_SECONDS)


async def _capture_window_state(session) -> str | None:
    """Best-effort window state used only to preserve minimize across watchdog restarts."""
    if session is None:
        return None
    capture = getattr(session, "capture_window_state", None)
    if capture is None:
        return None
    try:
        state = capture()
        if asyncio.iscoroutine(state):
            state = await asyncio.wait_for(state, timeout=2.0)
        state = str(state or "").lower()
        return state if state in {"minimized", "maximized", "fullscreen", "normal"} else None
    except Exception:  # noqa: BLE001 - focus preservation must never block recovery
        return None


async def _start_session(
    profile: BrowserProfile,
    session_factory: SessionFactory,
    concurrency: int,
    cancel_event,
    heartbeat: Callable[[], Awaitable[None]] | None,
    startup_window_state: str | None = None,
):
    session = session_factory(profile)
    with contextlib.suppress(Exception):
        session.cancel_event = cancel_event
    with contextlib.suppress(Exception):
        session.startup_window_state = startup_window_state
    try:
        start = session.start
        try:
            parameters = inspect.signature(start).parameters.values()
            accepts_concurrency = any(
                p.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                or p.kind is inspect.Parameter.VAR_POSITIONAL
                for p in parameters
            )
        except (TypeError, ValueError):
            accepts_concurrency = False
        status = await _await_with_watchdog(
            start(concurrency) if accepts_concurrency else start(),
            session=session,
            timeout_seconds=SESSION_START_WATCHDOG_SECONDS,
            heartbeat=heartbeat,
        )
        return session, status
    except JobCancelledError:
        await _close_session(session)
        raise
    except Exception as exc:  # noqa: BLE001 - the caller decides whether to restart
        print(f"[watchdog] browser-start-failed error={type(exc).__name__}: {exc}")
        await _close_session(session)
        return None, ProfileStatus.ERROR


async def _run_group(
    profile: BrowserProfile,
    usernames: list[str],
    session_factory: SessionFactory,
    concurrency: int,
    results: dict[str, LookupResult],
    progress: JobProgress,
    cb: ProgressCallback | None,
    lock: asyncio.Lock,
    cancel_event=None,
) -> None:
    session = None

    async def heartbeat() -> None:
        await _emit(cb, progress)

    session, status = await _start_session(
        profile, session_factory, concurrency, cancel_event, heartbeat
    )
    try:
        # A renderer/profile-lock error while opening the first Creator page is transient. Recover
        # it here instead of labelling all 10,000 rows LOGIN_REQUIRED before the first lookup.
        startup_restart = 0
        while (
            status is not ProfileStatus.CONNECTED
            and not _login_status(status)
            and startup_restart < MAX_SESSION_RESTARTS_PER_CREATOR
        ):
            startup_restart += 1
            print(
                f"[watchdog] restarting-browser-on-start "
                f"attempt={startup_restart}/{MAX_SESSION_RESTARTS_PER_CREATOR}"
            )
            window_state = await _capture_window_state(session) or "minimized"
            await _close_session(session)
            session, status = await _start_session(
                profile,
                session_factory,
                concurrency,
                cancel_event,
                heartbeat,
                startup_window_state=window_state,
            )

        if status is not ProfileStatus.CONNECTED:
            code = _status_error_code(status)
            for name in usernames:
                async with lock:
                    result = LookupResult(
                        normalized_username=name,
                        status=RowStatus.FAILED,
                        error_code=code,
                        account_label=profile.profile_code,
                    )
                    results[name] = result
                    progress.failed += 1
                    progress.processed += 1
                    progress.last_username = name
                    progress.last_result = result
                await _emit(cb, progress)
            return

        restart_lock = asyncio.Lock()
        session_generation = 0

        async def restart_for_creator(name, res, restarts_used, observed_generation):
            """Replace the one shared context once; other lanes reuse that replacement."""
            nonlocal session, status, session_generation
            async with restart_lock:
                if session_generation != observed_generation:
                    return res, restarts_used, True

                while restarts_used < MAX_SESSION_RESTARTS_PER_CREATOR:
                    restarts_used += 1
                    print(
                        f"[watchdog] restarting-browser creator={name} "
                        f"attempt={restarts_used}/{MAX_SESSION_RESTARTS_PER_CREATOR} "
                        f"reason={_error_code_name(res)}"
                    )
                    window_state = await _capture_window_state(session) or "minimized"
                    await _close_session(session)
                    session, status = await _start_session(
                        profile,
                        session_factory,
                        concurrency,
                        cancel_event,
                        heartbeat,
                        startup_window_state=window_state,
                    )
                    session_generation += 1
                    if status is ProfileStatus.CONNECTED and session is not None:
                        return res, restarts_used, True
                    res = _failed_lookup(
                        profile,
                        name,
                        _status_error_code(status),
                        "browser session could not reconnect automatically",
                    )
                    if _login_status(status):
                        break
                return res, restarts_used, False

        async def worker(name: str) -> None:
            nonlocal session
            async with lock:
                progress.current = name
            # Persist/show the current creator before the browser call. Heartbeats repeat this
            # checkpoint during legitimate long work so the UI can distinguish work from a crash.
            await _emit(cb, progress)

            res = None
            restarts_used = 0
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise JobCancelledError
                try:
                    observed_generation = session_generation
                    res = await _await_with_watchdog(
                        session.search_creator(name),
                        session=session,
                        timeout_seconds=LOOKUP_WATCHDOG_SECONDS,
                        heartbeat=heartbeat,
                    )
                except JobCancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - timeout/renderer crash recovery
                    res = _failed_lookup(
                        profile,
                        name,
                        ErrorCode.BROWSER_ERROR,
                        f"watchdog: {type(exc).__name__}: {exc}",
                    )

                needs_restart = _error_code_name(res) in _SESSION_RECOVERY_ERRORS
                if not needs_restart or restarts_used >= MAX_SESSION_RESTARTS_PER_CREATOR:
                    break

                res, restarts_used, reconnected = await restart_for_creator(
                    name, res, restarts_used, observed_generation
                )
                if not reconnected:
                    break

            if res is None:  # defensive: the bounded loop always assigns a result
                res = _failed_lookup(
                    profile, name, ErrorCode.BROWSER_ERROR, "watchdog produced no result"
                )
            async with lock:
                results[name] = res
                progress.processed += 1
                if res.status is RowStatus.SUCCESS:
                    progress.success += 1
                elif res.status is RowStatus.RANGE:
                    progress.range += 1
                else:
                    progress.failed += 1
                progress.last_username = name
                progress.last_result = res
            await _emit(cb, progress)

        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def bounded_worker(name: str) -> None:
            async with semaphore:
                # Cooperative cancel checkpoint before another creator enters a browser lane.
                if cancel_event is not None and cancel_event.is_set():
                    raise JobCancelledError
                await worker(name)

        outcomes = await asyncio.gather(
            *(bounded_worker(name) for name in usernames), return_exceptions=True
        )
        for outcome in outcomes:
            if isinstance(outcome, JobCancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                raise outcome
    finally:
        await _close_session(session)


async def run_job(
    input_path: str,
    output_path: str,
    *,
    session_factory: SessionFactory,
    selected_profile_code: str | None = None,
    concurrency: int = 1,
    sheets: list[str] | None = None,
    only_usernames: list[str] | None = None,
    prior_results: dict[str, LookupResult] | None = None,
    progress_cb: ProgressCallback | None = None,
    cancel_event=None,
) -> JobResult:
    """Run a full GMV job and write a format-preserving output workbook (spec §11).

    ``prior_results`` (used by retry, spec §15) seeds results that are NOT re-searched, so a
    retry of only-failed rows still preserves earlier successes in the output workbook.

    ``cancel_event`` (spec §6-§7): when set, the run stops cooperatively at the next checkpoint.
    Already-processed rows are kept; not-yet-processed rows are recorded as CANCELLED; a partial
    workbook is still written and ``JobResult.cancelled`` is True.
    """
    concurrency = min(get_max_concurrency(), max(1, int(concurrency)))
    plan = read_workbook(input_path, sheets)
    groups = _assign_profiles(plan, selected_profile_code)

    if only_usernames is not None:
        retry = set(only_usernames)
        groups = {code: [n for n in names if n in retry] for code, names in groups.items()}
        groups = {code: names for code, names in groups.items() if names}

    total = sum(len(names) for names in groups.values())
    progress = JobProgress(total=total)
    results: dict[str, LookupResult] = dict(prior_results or {})
    lock = asyncio.Lock()

    cancelled = False
    try:
        for code, names in groups.items():
            await _run_group(
                get_profile(code), names, session_factory, concurrency, results, progress,
                progress_cb, lock, cancel_event,
            )
    except JobCancelledError:
        cancelled = True

    if cancelled:
        # Mark every not-yet-processed creator (including the one interrupted) as CANCELLED,
        # keeping all completed rows intact (spec §7).
        for names in groups.values():
            for name in names:
                if name not in results:
                    results[name] = LookupResult(
                        normalized_username=name,
                        status=RowStatus.CANCELLED,
                        account_label=selected_profile_code or get_default_profile().profile_code,
                    )

    out = write_results(input_path, plan, results, output_path)
    return JobResult(plan=plan, results=results, output_path=out, cancelled=cancelled)

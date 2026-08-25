"""Durable Invitation Acceptor runner with pause, resume, retry and checkpoints."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

from gmv.config import ProfileStatus
from gmv.invitation_acceptor import InvitationSpec, write_invitation_accept_results
from gmv.models import JobCancelledError
from gmv.store import InvitationCreatorRecord, JobLogRecord, JobStatus, JobStore

SESSION_RESTARTS = 2
ITEM_WATCHDOG_SECONDS = 180.0
SUCCESS_STATES = {"SUCCESS", "ALREADY_ACCEPTED"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def append_log(record, message: str, status: str = "INFO") -> None:
    record.logs.append(JobLogRecord(time=now(), message=str(message), status=status))
    if len(record.logs) > 500:
        record.logs = record.logs[-500:]


def spec_from_state(state) -> InvitationSpec:
    return InvitationSpec(
        order=state.order,
        full_name=state.invitation_name,
        owner=state.owner,
        product=state.product,
        date=state.date,
        number=state.number,
    )


def creator_is_member(creator, candidates, invited, fallback_keys) -> bool:
    """Match by exact ID/username, then by a nickname unique on both sides."""
    if creator.keys & fallback_keys:
        return True
    if not candidates:
        return False
    if any(creator.keys & candidate.keys for candidate in candidates):
        return True
    nickname = creator.nickname.strip().casefold()
    if not nickname:
        return False
    invited_matches = sum(item.nickname.strip().casefold() == nickname for item in invited)
    candidate_matches = sum(item.nickname.strip().casefold() == nickname for item in candidates)
    return invited_matches == 1 and candidate_matches == 1


def checkpoint(store: JobStore, record) -> None:
    terminal = [state for state in record.invitation_accept_states if state.status not in {"QUEUED", "PROCESSING"}]
    record.processed = len(terminal)
    record.success = sum(state.status == "SUCCESS" for state in terminal)
    record.failed = sum(state.status not in SUCCESS_STATES for state in terminal)
    write_invitation_accept_results(
        record.invitation_accept_states,
        str(store.output_path(record.id)),
        record.invitation_creator_rows,
    )
    store.save(record)


async def close_session(session) -> None:
    if session is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(session.close(), timeout=10)


async def process_invitation_accept_job(
    store: JobStore,
    session_factory,
    profile,
    job_id: str,
    *,
    cancel_event=None,
    pause_event=None,
) -> None:
    record = store.get(job_id)
    if record is None:
        raise KeyError(job_id)
    record.status = JobStatus.RUNNING
    record.started_at = record.started_at or now()
    append_log(record, "초대장 Creator details 조회를 시작했습니다.", "PROCESSING")
    checkpoint(store, record)
    session = None
    cancelled = False
    try:
        for state in sorted(record.invitation_accept_states, key=lambda item: item.order):
            if state.status in SUCCESS_STATES:
                continue
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            if pause_event is not None:
                await pause_event.wait()
                if record.status == JobStatus.PAUSED:
                    record.status = JobStatus.RUNNING
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break

            state.status = "PROCESSING"
            state.message = None
            record.current = state.invitation_name
            record.current_phase = "search"
            record.search_page = 0
            append_log(record, f"{state.product} 검색 시작 · {state.invitation_name}", "PROCESSING")
            checkpoint(store, record)
            outcome = None
            last_error = None

            async def progress(phase, message, page=0, total=0):
                record.current_phase = phase
                record.search_page = page
                record.search_total_pages = total
                append_log(record, message, "PROCESSING")
                store.save(record)

            for attempt in range(SESSION_RESTARTS + 1):
                try:
                    if session is None:
                        session = session_factory(profile)
                        status = await asyncio.wait_for(session.start(), timeout=90)
                        if status in {ProfileStatus.LOGIN_REQUIRED, ProfileStatus.EXPIRED}:
                            state.status = "QUEUED"
                            state.message = "TikTok 로그인이 필요합니다. 로그인 후 작업 계속을 누르세요."
                            record.status = JobStatus.NEEDS_LOGIN
                            record.current_phase = "login_required"
                            append_log(record, state.message, "LOGIN_REQUIRED")
                            checkpoint(store, record)
                            return
                        if status is not ProfileStatus.CONNECTED:
                            raise RuntimeError(f"browser start status: {status}")
                    outcome = await asyncio.wait_for(
                        session.inspect_invitation(spec_from_state(state), progress),
                        timeout=ITEM_WATCHDOG_SECONDS,
                    )
                    break
                except JobCancelledError:
                    cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    code = getattr(exc, "error_code", "UNKNOWN_ERROR")
                    deterministic = code in {
                        "NOT_FOUND",
                        "MARKET_MISMATCH",
                        "DETAIL_FAILED",
                        "CREATOR_DETAILS_FAILED",
                        "INVITED_CREATORS_FAILED",
                        "ADDED_PRODUCTS_FAILED",
                        "POSTED_CONTENT_FAILED",
                    }
                    await close_session(session)
                    session = None
                    if deterministic or attempt == SESSION_RESTARTS:
                        break
                    append_log(record, f"브라우저 재연결 {attempt + 1}/{SESSION_RESTARTS}", "RETRY")

            if cancelled:
                break
            state.processed_at = now()
            if outcome is not None:
                state.status = outcome.status
                state.message = outcome.message
                state.invited_count = len(outcome.creators)
                state.added_products_count = len(outcome.added_creators)
                state.posted_content_count = len(outcome.posted_creators)
                record.invitation_creator_rows = [
                    row for row in record.invitation_creator_rows if row.order != state.order
                ]
                for creator in outcome.creators:
                    record.invitation_creator_rows.append(
                        InvitationCreatorRecord(
                            order=state.order,
                            keyword=state.product,
                            invitation_name=state.invitation_name,
                            creator=creator.creator,
                            nickname=creator.nickname or None,
                            creator_id=creator.creator_id or None,
                            region=creator.region or state.market,
                            market=state.market,
                            added_products=creator_is_member(
                                creator,
                                outcome.added_creators,
                                outcome.creators,
                                outcome.added_product_keys,
                            ),
                            posted_content=creator_is_member(
                                creator,
                                outcome.posted_creators,
                                outcome.creators,
                                outcome.posted_content_keys,
                            ),
                        )
                    )
                record.unique_creators = len(record.invitation_creator_rows)
                append_log(record, f"{state.invitation_name} · {outcome.status}", outcome.status)
            else:
                state.status = getattr(last_error, "error_code", "UNKNOWN_ERROR")
                state.message = str(last_error or "알 수 없는 오류")[:500]
                append_log(record, f"{state.invitation_name} · {state.status}: {state.message}", "ERROR")
            if pause_event is not None and not pause_event.is_set():
                record.status = JobStatus.PAUSED
            checkpoint(store, record)
    finally:
        await close_session(session)

    if cancelled:
        for state in record.invitation_accept_states:
            if state.status in {"QUEUED", "PROCESSING"}:
                state.status = "CANCELLED"
                state.message = "사용자가 작업을 중지했습니다."
        record.status = JobStatus.CANCELLED
        append_log(record, "작업이 중지되었습니다.", "CANCELLED")
    else:
        record.status = (
            JobStatus.COMPLETED
            if all(state.status in SUCCESS_STATES for state in record.invitation_accept_states)
            else JobStatus.COMPLETED_WITH_ERRORS
        )
        append_log(record, "전체 초대장 Creator details 조회가 완료되었습니다.", "COMPLETED")
    record.current = None
    record.current_phase = None
    record.finished_at = now()
    record.output_filename = "invitation_creator_results.xlsx"
    checkpoint(store, record)

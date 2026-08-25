"""Worker HTTP API (spec §1, §9, §10, §12, §13, §15 + cancel §5-§8).

FastAPI app the web front-end calls. Browser automation runs here (never on Vercel, spec §8).
The session factory is injected via app state so tests can supply a fake (no real browser).

Login, verify and reset mutate a persistent browser profile and are serialized per profile. GMV
jobs briefly snapshot that source, then run from independent disposable profiles, so jobs sharing
one TikTok login can execute concurrently without Chromium profile-lock conflicts. A running job
may still be stopped cooperatively via its per-job ``asyncio.Event``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from gmv.config import (
    default_job_root,
    get_default_profile,
    get_profile,
    list_profiles,
)
from gmv.excel_engine import read_workbook
from gmv.invitation_acceptor import expand_invitation_input, write_invitation_accept_results
from gmv.invitation_acceptor_service import process_invitation_accept_job
from gmv.models import RowStatus
from gmv.profile_runtime import cleanup_stale_runtime_profiles, cloned_profile_for_job
from gmv.service import UploadValidationError, process_job, validate_upload
from gmv.store import InvitationAcceptRecord, JobRecord, JobStatus, JobStore

WORKER_BUILD_ID = "invitation-creators-v25"

# A Job is the unit that owns a visible automation browser. Creator-level parallelism used to
# create extra tabs inside that browser, which made one upload look like several automations.
# Keep one page per Job; users get another independent browser by starting another Job instead.
PAGES_PER_JOB = 1

_SANITIZE_KEYS = {
    "browser_channel",
    "market",
    "shop_region",
    "profile_storage_path",
    "affiliate_url",
    "affiliate_entry_url",
    "login_url",
    "login_success_url",
    "find_creators_url",
}

_INTERRUPTED_STATUSES = {
    JobStatus.VALIDATING,
    JobStatus.QUEUED,
    JobStatus.RUNNING,
    JobStatus.CANCEL_REQUESTED,
    JobStatus.MERGING,
    JobStatus.PAUSED,
}


class InvitationAcceptJobRequest(BaseModel):
    profile_code: str = "US_CHROME"
    invitation_text: str

_FINISHED_STATUSES = {
    JobStatus.COMPLETED,
    JobStatus.COMPLETED_WITH_ERRORS,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
    JobStatus.NEEDS_LOGIN,
}


def _sanitize_profile_payload(payload: dict) -> dict:
    """Drop operator-only fields (browser/market/region/URLs/paths) from the client view (§10)."""
    return {k: v for k, v in payload.items() if k not in _SANITIZE_KEYS}


@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    await asyncio.to_thread(cleanup_stale_runtime_profiles)
    try:
        yield
    finally:
        # Give cooperative cancellation a bounded window so active clone contexts can clean up.
        for event in app.state.job_cancel_events.values():
            event.set()
        for event in app.state.job_pause_events.values():
            event.set()
        tasks = list(app.state.tasks)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5.0)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


def _busy_conflict() -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": "현재 다른 TikTok 작업이 실행 중입니다. 완료 후 다시 시도하세요.",
            "code": "BROWSER_BUSY",
        },
    )


def real_session_factory(profile):
    from gmv.automation.session import TikTokAffiliateSession

    headless = os.environ.get("GMV_HEADLESS", "0") == "1"
    return TikTokAffiliateSession(profile, headless=headless)


def real_invitation_acceptor_factory(profile):
    from gmv.automation.invitation_inspector_session import TikTokInvitationInspectorSession

    headless = os.environ.get("GMV_HEADLESS", "0") == "1"
    return TikTokInvitationInspectorSession(profile, headless=headless)


def _recover_interrupted_jobs(store: JobStore) -> None:
    """Mark jobs left mid-flight by a crash/restart as failed (spec §12.7)."""
    for record in store.list():
        if record.status in _INTERRUPTED_STATUSES:
            # Invitation Acceptor is explicitly resumable after a Worker/page restart.
            record.status = (
                JobStatus.PAUSED
                if record.job_type == "INVITATION_ACCEPT"
                else JobStatus.FAILED
            )
            record.current = None
            store.save(record)


def _uploaded_rows_from_workbook(store: JobStore, record: JobRecord) -> list[dict]:
    """Read the visible row skeleton once; callers cache it while the worker is running."""
    input_path = store.input_path(record.id)
    if not input_path.exists():
        return []
    plan = read_workbook(str(input_path))
    return [
        {
            "sheet_name": row.sheet_name,
            "excel_row_number": row.excel_row_number,
            "creator": row.original_creator_value,
            "normalized_username": row.normalized_username,
            "status": "PENDING",
            "gmv_value": None,
            "items_sold": None,
            "error_code": None,
            "error_message": None,
        }
        for sheet in plan.sheets
        for row in sheet.rows
    ]


def _job_payload_with_uploaded_rows(
    store: JobStore,
    record: JobRecord,
    uploaded_rows: list[dict] | None = None,
) -> dict:
    """Return a job payload whose row list mirrors the uploaded Excel immediately.

    ``process_job`` replaces/updates stored rows as lookups finish. Before that happens the
    record may contain no rows, which used to leave the result table completely blank during
    TikTok verification and the first lookup. Build the missing pending rows from the original
    workbook at read time, then overlay every stored result by its sheet/Excel-row identity.
    This keeps the input order and also works for duplicate creator names.
    """
    payload = record.model_dump()
    if uploaded_rows is None:
        try:
            uploaded_rows = _uploaded_rows_from_workbook(store, record)
        except Exception:  # noqa: BLE001 - a job response must work if its input was removed
            return payload

    stored_rows = payload.get("rows") or []
    by_position = {
        (row.get("sheet_name"), row.get("excel_row_number")): row
        for row in stored_rows
        if isinstance(row, dict)
    }
    visible_rows: list[dict] = []
    for row in uploaded_rows:
        key = (row.get("sheet_name"), row.get("excel_row_number"))
        visible_rows.append(by_position.get(key, row))
    payload["rows"] = visible_rows
    return payload


def create_app(
    store: JobStore | None = None,
    session_factory=real_session_factory,
    invitation_acceptor_session_factory=real_invitation_acceptor_factory,
) -> FastAPI:
    app = FastAPI(title="GMV Worker", version="0.1.0", lifespan=_app_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.store = store or JobStore(
        os.environ.get("GMV_STORAGE_ROOT", str(default_job_root()))
    )
    app.state.session_factory = session_factory
    app.state.invitation_acceptor_session_factory = invitation_acceptor_session_factory
    app.state.tasks = set()  # strong refs so background tasks are not GC'd mid-run
    # Uploaded creator rows are cached separately from lookup results. This makes polling a
    # 780-row result page cheap while still allowing the stored completed rows to overlay it.
    app.state.job_uploaded_rows = {}
    # Chrome/US and Edge/UK use different persistent profile directories, so they need separate
    # mutation reservations. Normal jobs use clones and are not members of this set.
    app.state.busy_profiles = set()
    app.state.profile_source_locks = {
        profile.profile_code: asyncio.Lock() for profile in list_profiles()
    }
    # Per-job cooperative cancel signals (spec §6).
    app.state.job_cancel_events = {}
    app.state.job_pause_events = {}
    app.state.invitation_accept_active_jobs = set()
    _recover_interrupted_jobs(app.state.store)

    from gmv.login_manager import recover_interrupted_statuses

    recover_interrupted_statuses()

    def _reserve(code: str) -> bool:
        if code in app.state.busy_profiles:
            return False
        app.state.busy_profiles.add(code)
        return True

    def _release(code: str) -> None:
        app.state.busy_profiles.discard(code)

    def _source_lock(code: str) -> asyncio.Lock:
        return app.state.profile_source_locks.setdefault(code, asyncio.Lock())

    async def _spawn_exclusive(code: str, coro) -> None:
        lock = _source_lock(code)
        try:
            await lock.acquire()
        except BaseException:
            _release(code)
            if hasattr(coro, "close"):
                coro.close()
            raise

        async def runner():
            try:
                await coro
            finally:
                _release(code)
                lock.release()

        runner_coro = runner()
        try:
            _spawn(app, runner_coro)
        except BaseException:
            runner_coro.close()
            _release(code)
            lock.release()
            if hasattr(coro, "close"):
                coro.close()
            raise

    @app.get("/health")
    async def health():
        # Lets the launcher replace a stale Worker instead of silently reusing old code.
        return {
            "ok": True,
            "build": WORKER_BUILD_ID,
            "mode": "fast" if os.environ.get("GMV_FAST_MODE") == "1" else "normal",
        }

    # ---- Profile status (single default profile, spec §10) ----

    @app.get("/profiles")
    async def profiles():
        from gmv.login_manager import read_status

        return [read_status(p.profile_code) for p in list_profiles()]

    @app.get("/profile")
    async def profile():
        from gmv.login_manager import read_status

        return _sanitize_profile_payload(read_status(get_default_profile().profile_code))

    @app.get("/profiles/{code}")
    async def profile_status(code: str):
        from gmv.login_manager import read_status

        try:
            return read_status(code)
        except KeyError as exc:
            raise HTTPException(404, "unknown profile") from exc

    # ---- Login / verify / reset (all share the browser, all gated, spec §12) ----

    async def _do_login(code: str):
        from gmv.login_manager import open_login

        try:
            profile = get_profile(code)
        except KeyError as exc:
            raise HTTPException(404, "unknown profile") from exc
        if not _reserve(profile.profile_code):
            return _busy_conflict()
        await _spawn_exclusive(profile.profile_code, open_login(profile.profile_code))
        return {"profile_code": profile.profile_code, "status": "connecting"}

    async def _do_verify(code: str):
        from gmv.login_manager import verify

        try:
            profile = get_profile(code)
        except KeyError as exc:
            raise HTTPException(404, "unknown profile") from exc
        if not _reserve(profile.profile_code):
            return _busy_conflict()
        await _spawn_exclusive(profile.profile_code, verify(profile.profile_code))
        return {"profile_code": profile.profile_code, "status": "verifying"}

    async def _do_reset(code: str):
        from gmv.login_manager import reset

        try:
            profile = get_profile(code)
        except KeyError as exc:
            raise HTTPException(404, "unknown profile") from exc
        if not _reserve(profile.profile_code):
            return _busy_conflict()
        lock = _source_lock(profile.profile_code)
        try:
            async with lock:
                return await asyncio.to_thread(reset, profile.profile_code)
        finally:
            _release(profile.profile_code)

    @app.post("/profiles/{code}/login")
    async def profile_login(code: str):
        return await _do_login(code)

    @app.post("/profiles/{code}/verify")
    async def profile_verify(code: str):
        return await _do_verify(code)

    @app.post("/profiles/{code}/reset")
    async def profile_reset(code: str):
        return await _do_reset(code)

    @app.post("/profile/login")
    async def profile_login_alias():
        return await _do_login(get_default_profile().profile_code)

    @app.post("/profile/verify")
    async def profile_verify_alias():
        return await _do_verify(get_default_profile().profile_code)

    @app.post("/profile/reset")
    async def profile_reset_alias():
        return await _do_reset(get_default_profile().profile_code)

    # ---- Jobs ----

    @app.post("/jobs")
    async def create_job(
        file: UploadFile,
        profile_code: str | None = Form(None),
        concurrency: int | None = Form(None),
    ):
        store: JobStore = app.state.store

        if file.filename is None or not file.filename.lower().endswith(".xlsx"):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": ".xlsx 형식의 Excel 파일만 업로드할 수 있습니다.",
                    "code": "INVALID_FILE_TYPE",
                },
            )

        try:
            profile = get_profile(profile_code or get_default_profile().profile_code)
        except KeyError:
            return JSONResponse(
                status_code=400,
                content={"detail": "지원하지 않는 브라우저/마켓입니다.", "code": "INVALID_PROFILE"},
            )

        # Keep accepting the legacy form field so old cached web pages remain compatible, but do
        # not let it fan one Job out into multiple TikTok tabs. Parallel work is represented by
        # multiple Job records, each of which receives its own runtime profile and browser.
        _ = concurrency
        record = JobRecord(
            original_filename=file.filename or "upload.xlsx",
            selected_profile_code=profile.profile_code,
            concurrency=PAGES_PER_JOB,
            status=JobStatus.VALIDATING,
        )
        store.save(record)
        store.input_path(record.id).write_bytes(await file.read())

        try:
            total, unique = validate_upload(store, record.id)
        except UploadValidationError as exc:
            store.delete(record.id)  # clean up the rejected upload (spec §13)
            return JSONResponse(status_code=400, content={"detail": exc.detail, "code": exc.code})
        except Exception:  # noqa: BLE001
            store.delete(record.id)
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "Excel 파일을 열 수 없습니다. 파일이 손상되었거나 지원되지 않는 형식입니다.",
                    "code": "INVALID_EXCEL",
                },
            )

        record.total_rows, record.unique_creators = total, unique
        record.status = JobStatus.QUEUED
        store.save(record)
        try:
            app.state.job_uploaded_rows[record.id] = _uploaded_rows_from_workbook(store, record)
        except Exception:  # validation already succeeded; fall back to lazy read in GET
            pass

        cancel_event = asyncio.Event()
        app.state.job_cancel_events[record.id] = cancel_event
        _spawn(app, _run(app, record.id, cancel_event))
        return {"job_id": record.id, "total_rows": total, "unique_creators": unique}

    @app.get("/jobs")
    async def list_jobs():
        return [r.model_dump() for r in app.state.store.list() if r.job_type == "GMV"]

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str):
        record = app.state.store.get(job_id)
        if record is None:
            raise HTTPException(404, "job not found")
        uploaded_rows = app.state.job_uploaded_rows.get(job_id)
        if uploaded_rows is None:
            try:
                uploaded_rows = _uploaded_rows_from_workbook(app.state.store, record)
                app.state.job_uploaded_rows[job_id] = uploaded_rows
            except Exception:  # handled by the payload helper below
                uploaded_rows = None
        return _job_payload_with_uploaded_rows(app.state.store, record, uploaded_rows)

    @app.post("/jobs/{job_id}/retry")
    async def retry_job(job_id: str):
        store: JobStore = app.state.store
        record = store.get(job_id)
        if record is None:
            raise HTTPException(404, "job not found")

        if record.status not in _FINISHED_STATUSES:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "실행 중인 작업은 재조회할 수 없습니다. 먼저 작동 중지를 완료하세요.",
                    "code": "JOB_STILL_RUNNING",
                },
            )

        # ``process_job(retry_failed=True)`` already preserves successful/range rows and only
        # searches FAILED rows. A cooperative stop records every untouched creator as CANCELLED,
        # so promote all incomplete row states to FAILED before starting the existing retry path.
        # This gives us true resume semantics without ever re-querying completed creators.
        retryable_states = {
            RowStatus.FAILED.value,
            RowStatus.CANCELLED.value,
            "PENDING",
            "RUNNING",
        }
        # Older interrupted runs persisted counters but not row details. Because those values do
        # not exist on disk, their only correct recovery is a full rerun of the same input file.
        full_restart = not record.rows
        resumed_rows = record.unique_creators if full_restart else 0
        if not full_restart:
            for row in record.rows:
                row_status = getattr(row.status, "value", row.status)
                if str(row_status) not in retryable_states:
                    continue
                row.status = RowStatus.FAILED.value
                row.error_code = None
                row.error_message = None
                resumed_rows += 1

        if resumed_rows == 0:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "재조회할 중지·대기·실패 행이 없습니다.",
                    "code": "NO_ROWS_TO_RETRY",
                },
            )

        # Persist QUEUED before returning so the result page immediately resumes polling instead
        # of remaining in the terminal CANCELLED state until the background task gets CPU time.
        record.status = JobStatus.QUEUED
        record.current = None
        record.concurrency = PAGES_PER_JOB
        store.save(record)
        cancel_event = asyncio.Event()
        app.state.job_cancel_events[job_id] = cancel_event
        _spawn(app, _run(app, job_id, cancel_event, retry=not full_restart))
        return {
            "job_id": job_id,
            "status": "retrying",
            "resumed_rows": resumed_rows,
            "full_restart": full_restart,
        }

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        store: JobStore = app.state.store
        record = store.get(job_id)
        if record is None:
            return JSONResponse(
                status_code=404,
                content={"detail": "작업을 찾을 수 없습니다.", "code": "JOB_NOT_FOUND"},
            )
        if record.status in _FINISHED_STATUSES:
            return JSONResponse(
                status_code=409,
                content={
                    "id": record.id,
                    "status": record.status,
                    "detail": "이미 종료된 작업입니다.",
                    "code": "JOB_ALREADY_FINISHED",
                },
            )
        # queued / running / cancel_requested -> signal the loop and mark the request.
        event = app.state.job_cancel_events.get(job_id)
        if event is not None:
            event.set()
        record.status = JobStatus.CANCEL_REQUESTED
        store.save(record)
        return {
            "id": record.id,
            "status": JobStatus.CANCEL_REQUESTED,
            "message": "작업 중지를 요청했습니다.",
        }

    @app.get("/jobs/{job_id}/download")
    async def download(job_id: str):
        store: JobStore = app.state.store
        record = store.get(job_id)
        if record is None:
            raise HTTPException(404, "job not found")
        out = store.output_path(job_id)
        if not out.exists():
            raise HTTPException(409, "output not ready")
        return FileResponse(
            out,
            filename=record.output_filename or "result.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    # ---- Invitation Acceptor ----

    @app.post("/invitation-accept/parse")
    async def parse_invitation_accept_input(request: InvitationAcceptJobRequest):
        specs, errors = expand_invitation_input(request.invitation_text)
        return {
            "items": [
                {
                    "order": spec.order,
                    "invitation_name": spec.full_name,
                    "owner": spec.owner,
                    "product": spec.product,
                    "date": spec.date,
                    "number": spec.number,
                }
                for spec in specs
            ],
            "errors": errors,
        }

    @app.post("/invitation-accept-jobs")
    async def create_invitation_accept_job(request: InvitationAcceptJobRequest):
        try:
            profile = get_profile(request.profile_code)
        except KeyError:
            return JSONResponse(
                status_code=400,
                content={"detail": "지원하지 않는 브라우저/마켓입니다.", "code": "INVALID_PROFILE"},
            )
        specs, errors = expand_invitation_input(request.invitation_text)
        if errors or not specs:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": " / ".join(errors) if errors else "수락할 초대장명을 입력하세요.",
                    "code": "INVALID_INVITATION_INPUT",
                    "errors": errors,
                },
            )
        record = JobRecord(
            original_filename="invitation_acceptor.txt",
            selected_profile_code=profile.profile_code,
            concurrency=PAGES_PER_JOB,
            status=JobStatus.QUEUED,
            job_type="INVITATION_ACCEPT",
            total_rows=len(specs),
            unique_invitations=len(specs),
            invitation_accept_states=[
                InvitationAcceptRecord(
                    order=spec.order,
                    invitation_name=spec.full_name,
                    owner=spec.owner,
                    product=spec.product,
                    date=spec.date,
                    number=spec.number,
                    market=profile.market,
                )
                for spec in specs
            ],
        )
        app.state.store.save(record)
        app.state.store.input_text_path(record.id).write_text(request.invitation_text, encoding="utf-8")
        await asyncio.to_thread(
            write_invitation_accept_results,
            record.invitation_accept_states,
            str(app.state.store.output_path(record.id)),
        )
        cancel_event = asyncio.Event()
        pause_event = asyncio.Event()
        pause_event.set()
        app.state.job_cancel_events[record.id] = cancel_event
        app.state.job_pause_events[record.id] = pause_event
        app.state.invitation_accept_active_jobs.add(record.id)
        _spawn(app, _run_invitation_accept(app, record.id, cancel_event, pause_event))
        return {"job_id": record.id, "total": len(specs)}

    @app.get("/invitation-accept-jobs")
    async def list_invitation_accept_jobs():
        return [
            record.model_dump()
            for record in app.state.store.list()
            if record.job_type == "INVITATION_ACCEPT"
        ]

    @app.get("/invitation-accept-jobs/{job_id}")
    async def get_invitation_accept_job(job_id: str):
        record = app.state.store.get(job_id)
        if record is None or record.job_type != "INVITATION_ACCEPT":
            raise HTTPException(404, "invitation accept job not found")
        return record.model_dump()

    @app.post("/invitation-accept-jobs/{job_id}/pause")
    async def pause_invitation_accept_job(job_id: str):
        record = app.state.store.get(job_id)
        if record is None or record.job_type != "INVITATION_ACCEPT":
            raise HTTPException(404, "invitation accept job not found")
        if record.status not in {JobStatus.RUNNING, JobStatus.QUEUED}:
            return JSONResponse(status_code=409, content={"detail": "일시정지할 작업이 아닙니다."})
        event = app.state.job_pause_events.get(job_id)
        if event is not None:
            event.clear()
        record.status = JobStatus.PAUSED
        app.state.store.save(record)
        return {"job_id": job_id, "status": JobStatus.PAUSED}

    @app.post("/invitation-accept-jobs/{job_id}/resume")
    async def resume_invitation_accept_job(job_id: str):
        record = app.state.store.get(job_id)
        if record is None or record.job_type != "INVITATION_ACCEPT":
            raise HTTPException(404, "invitation accept job not found")
        if record.status not in {JobStatus.PAUSED, JobStatus.NEEDS_LOGIN}:
            return JSONResponse(status_code=409, content={"detail": "계속할 수 있는 작업이 아닙니다."})
        record.status = JobStatus.RUNNING
        app.state.store.save(record)
        event = app.state.job_pause_events.get(job_id)
        if job_id in app.state.invitation_accept_active_jobs and event is not None:
            event.set()
            return {"job_id": job_id, "status": JobStatus.RUNNING}
        cancel_event = asyncio.Event()
        pause_event = asyncio.Event()
        pause_event.set()
        app.state.job_cancel_events[job_id] = cancel_event
        app.state.job_pause_events[job_id] = pause_event
        app.state.invitation_accept_active_jobs.add(job_id)
        _spawn(app, _run_invitation_accept(app, job_id, cancel_event, pause_event))
        return {"job_id": job_id, "status": JobStatus.RUNNING}

    @app.post("/invitation-accept-jobs/{job_id}/cancel")
    async def cancel_invitation_accept_job(job_id: str):
        record = app.state.store.get(job_id)
        if record is None or record.job_type != "INVITATION_ACCEPT":
            raise HTTPException(404, "invitation accept job not found")
        if record.status in _FINISHED_STATUSES:
            return JSONResponse(status_code=409, content={"detail": "이미 종료된 작업입니다."})
        event = app.state.job_cancel_events.get(job_id)
        if event is not None:
            event.set()
        pause_event = app.state.job_pause_events.get(job_id)
        if pause_event is not None:
            pause_event.set()
        record.status = JobStatus.CANCEL_REQUESTED
        app.state.store.save(record)
        return {"job_id": job_id, "status": JobStatus.CANCEL_REQUESTED}

    @app.post("/invitation-accept-jobs/{job_id}/retry")
    async def retry_invitation_accept_job(job_id: str):
        record = app.state.store.get(job_id)
        if record is None or record.job_type != "INVITATION_ACCEPT":
            raise HTTPException(404, "invitation accept job not found")
        if record.status not in _FINISHED_STATUSES:
            return JSONResponse(status_code=409, content={"detail": "실행 중인 작업입니다."})
        retryable = [
            state for state in record.invitation_accept_states if state.status not in {"SUCCESS", "ALREADY_ACCEPTED"}
        ]
        if not retryable:
            return JSONResponse(status_code=409, content={"detail": "재시도할 초대장이 없습니다."})
        for state in retryable:
            state.status = "QUEUED"
            state.message = None
            state.processed_at = None
        record.status = JobStatus.QUEUED
        record.finished_at = None
        app.state.store.save(record)
        cancel_event = asyncio.Event()
        pause_event = asyncio.Event()
        pause_event.set()
        app.state.job_cancel_events[job_id] = cancel_event
        app.state.job_pause_events[job_id] = pause_event
        app.state.invitation_accept_active_jobs.add(job_id)
        _spawn(app, _run_invitation_accept(app, job_id, cancel_event, pause_event))
        return {"job_id": job_id, "status": "retrying"}

    @app.get("/invitation-accept-jobs/{job_id}/download")
    async def download_invitation_accept_job(job_id: str):
        record = app.state.store.get(job_id)
        if record is None or record.job_type != "INVITATION_ACCEPT":
            raise HTTPException(404, "invitation accept job not found")
        output = app.state.store.output_path(job_id)
        if not output.exists():
            await asyncio.to_thread(
                write_invitation_accept_results,
                record.invitation_accept_states,
                str(output),
                record.invitation_creator_rows,
            )
        return FileResponse(
            output,
            filename=record.output_filename or "invitation_accept_partial.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    @app.exception_handler(KeyError)
    async def _key_error(_request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    return app


def _spawn(app: FastAPI, coro) -> None:
    task = asyncio.create_task(coro)
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)


async def _run(app: FastAPI, job_id: str, cancel_event=None, retry: bool = False) -> None:
    try:
        record = app.state.store.get(job_id)
        if record is None:
            raise KeyError(job_id)

        if not record.selected_profile_code:
            record.selected_profile_code = get_default_profile().profile_code

        # Also repair legacy/persisted Jobs before starting or retrying them. This is the final
        # safety boundary guaranteeing exactly one automation page/window for one Job.
        if record.concurrency != PAGES_PER_JOB:
            record.concurrency = PAGES_PER_JOB
        app.state.store.save(record)

        profile = get_profile(record.selected_profile_code)
        source_lock = app.state.profile_source_locks.setdefault(
            profile.profile_code, asyncio.Lock()
        )
        async with cloned_profile_for_job(
            profile,
            job_id,
            source_lock=source_lock,
        ) as runtime_profile:
            # Watchdog restarts reuse this job clone. Retrying the job enters the context again and
            # creates a fresh snapshot from the persistent login profile.
            def job_session_factory(_profile):
                return app.state.session_factory(runtime_profile)

            await process_job(
                app.state.store,
                job_session_factory,
                job_id,
                retry_failed=retry,
                cancel_event=cancel_event,
            )
    except Exception as exc:  # noqa: BLE001 - never crash the worker on a job error
        record = app.state.store.get(job_id)
        if record is not None:
            record.status = JobStatus.FAILED
            record.current = None
            app.state.store.save(record)
        print(f"[job {job_id}] failed: {type(exc).__name__}: {exc}")
    finally:
        # Clean up the cancel signal so the next job starts fresh (spec §8).
        app.state.job_cancel_events.pop(job_id, None)


async def _run_invitation_accept(
    app: FastAPI,
    job_id: str,
    cancel_event=None,
    pause_event=None,
) -> None:
    try:
        record = app.state.store.get(job_id)
        if record is None:
            raise KeyError(job_id)
        profile = get_profile(record.selected_profile_code or get_default_profile().profile_code)
        source_lock = app.state.profile_source_locks.setdefault(
            profile.profile_code, asyncio.Lock()
        )
        async with cloned_profile_for_job(
            profile,
            job_id,
            source_lock=source_lock,
        ) as runtime_profile:
            def acceptor_factory(_profile):
                session = app.state.invitation_acceptor_session_factory(runtime_profile)
                if getattr(session, "cancel_event", None) is None:
                    with contextlib.suppress(Exception):
                        session.cancel_event = cancel_event
                return session

            await process_invitation_accept_job(
                app.state.store,
                acceptor_factory,
                runtime_profile,
                job_id,
                cancel_event=cancel_event,
                pause_event=pause_event,
            )
    except Exception as exc:  # noqa: BLE001
        record = app.state.store.get(job_id)
        if record is not None:
            record.status = JobStatus.FAILED
            record.current = None
            app.state.store.save(record)
        print(f"[invitation-accept-job {job_id}] failed: {type(exc).__name__}: {exc}")
    finally:
        app.state.invitation_accept_active_jobs.discard(job_id)
        app.state.job_cancel_events.pop(job_id, None)
        app.state.job_pause_events.pop(job_id, None)

"""Job processing service: bridges the store, the runner and per-row records (spec §13,§15)."""

from __future__ import annotations

from datetime import datetime

from gmv.excel_engine import make_output_filename, read_workbook
from gmv.job_runner import run_job
from gmv.models import GmvValueType, LookupResult, ParsedGmv, RowStatus
from gmv.store import JobRecord, JobStatus, JobStore, RowRecord


class UploadValidationError(Exception):
    def __init__(self, detail: str, code: str):
        super().__init__(detail)
        self.detail = detail
        self.code = code


def _prior_results_from_record(record: JobRecord) -> dict[str, LookupResult]:
    """Rebuild LookupResults for already-successful rows (so retry keeps them)."""
    prior: dict[str, LookupResult] = {}
    for row in record.rows:
        if row.status in (RowStatus.SUCCESS.value, RowStatus.RANGE.value) and row.normalized_username:
            gmv = None
            if row.gmv_value is not None:
                gmv = ParsedGmv(
                    value=row.gmv_value,
                    raw_value=row.raw_gmv,
                    value_type=GmvValueType(row.gmv_type) if row.gmv_type else GmvValueType.EXACT,
                )
            prior[row.normalized_username] = LookupResult(
                normalized_username=row.normalized_username,
                gmv=gmv,
                items_sold=row.items_sold,
                status=RowStatus(row.status),
                account_label=row.account_code,
            )
    return prior


def _build_rows(plan, results: dict[str, LookupResult]) -> list[RowRecord]:
    rows: list[RowRecord] = []
    for sheet in plan.sheets:
        for r in sheet.rows:
            res = results.get(r.normalized_username or "")
            rec = RowRecord(
                sheet_name=r.sheet_name,
                excel_row_number=r.excel_row_number,
                creator=r.original_creator_value,
                normalized_username=r.normalized_username,
                account_code=r.account_code,
            )
            if res is not None:
                _apply_result_to_row(rec, res)
            rows.append(rec)
    return rows


def _apply_result_to_row(rec: RowRecord, res: LookupResult) -> None:
    """Copy one completed creator lookup into its durable row checkpoint."""
    rec.status = res.status.value
    rec.error_code = res.error_code.value if res.error_code else None
    rec.error_message = res.error_message
    rec.current_stage = res.current_stage
    rec.source = res.source
    rec.display_name = res.display_name
    rec.pps = res.pps
    rec.category = res.category
    rec.audience = res.audience
    rec.status_badge = res.status_badge
    rec.items_sold_raw = res.items_sold_raw
    rec.items_sold = res.items_sold
    rec.raw_gmv = None
    rec.gmv_type = None
    rec.gmv_value = None
    if res.gmv is not None:
        rec.raw_gmv = res.gmv.raw_value
        rec.gmv_type = res.gmv.value_type.value
        rec.gmv_value = float(res.gmv.value) if res.gmv.value is not None else None


async def process_job(
    store: JobStore,
    session_factory,
    job_id: str,
    *,
    retry_failed: bool = False,
    cancel_event=None,
) -> JobRecord:
    """Run (or retry) a job, updating the store as it goes (spec §13, §6-§7)."""
    record = store.get(job_id)
    if record is None:
        raise KeyError(job_id)

    record.status = JobStatus.RUNNING
    record.started_at = datetime.now().isoformat(timespec="seconds")
    plan = read_workbook(str(store.input_path(job_id)))
    if not record.rows:
        # Create the row skeleton before browser work. Each completed creator can now be saved
        # immediately instead of all details existing only in RAM until the workbook is finished.
        record.rows = _build_rows(plan, {})
    store.save(record)

    only = None
    prior = None
    if retry_failed:
        only = [
            row.normalized_username
            for row in record.rows
            if row.status == RowStatus.FAILED.value and row.normalized_username
        ]
        prior = _prior_results_from_record(record)

    async def on_progress(p) -> None:
        record.processed = p.processed
        record.success = p.success
        record.range_rows = p.range
        record.failed = p.failed
        record.current = p.current
        if p.last_username and p.last_result is not None:
            for row in record.rows:
                if row.normalized_username == p.last_username:
                    _apply_result_to_row(row, p.last_result)
        # Surface the pending stop to the UI while the current creator is wrapped up (§9).
        if cancel_event is not None and cancel_event.is_set() and record.status == JobStatus.RUNNING:
            record.status = JobStatus.CANCEL_REQUESTED
        store.save(record)

    result = await run_job(
        str(store.input_path(job_id)),
        str(store.output_path(job_id)),
        session_factory=session_factory,
        selected_profile_code=record.selected_profile_code,
        concurrency=record.concurrency,
        only_usernames=only,
        prior_results=prior,
        progress_cb=on_progress,
        cancel_event=cancel_event,
    )

    record.status = JobStatus.MERGING
    store.save(record)

    record.rows = _build_rows(result.plan, result.results)
    record.total_rows = len(record.rows)
    record.unique_creators = len(result.plan.unique_usernames())
    counts = {"success": 0, "range": 0, "failed": 0}
    for row in record.rows:
        if row.status == RowStatus.SUCCESS.value:
            counts["success"] += 1
        elif row.status == RowStatus.RANGE.value:
            counts["range"] += 1
        elif row.status == RowStatus.FAILED.value:
            counts["failed"] += 1
    record.processed = counts["success"] + counts["range"] + counts["failed"]
    record.success, record.range_rows, record.failed = (
        counts["success"],
        counts["range"],
        counts["failed"],
    )
    record.output_filename = make_output_filename(
        record.original_filename, partial=result.cancelled
    )
    record.finished_at = datetime.now().isoformat(timespec="seconds")

    login_errors = {"LOGIN_REQUIRED", "SESSION_EXPIRED"}
    if result.cancelled:
        # User stopped the job — keep partial results and offer the partial download (§7).
        record.status = JobStatus.CANCELLED
    elif record.rows and all(r.error_code in login_errors for r in record.rows):
        record.status = JobStatus.NEEDS_LOGIN
    elif counts["failed"] > 0:
        record.status = JobStatus.COMPLETED_WITH_ERRORS
    else:
        record.status = JobStatus.COMPLETED
    record.current = None
    store.save(record)
    return record


def _max_upload_bytes() -> int:
    import os

    try:
        mb = float(os.environ.get("GMV_MAX_UPLOAD_MB", "10"))
    except ValueError:
        mb = 10.0
    return int(mb * 1024 * 1024)


def validate_upload(store: JobStore, job_id: str) -> tuple[int, int]:
    """Validate an uploaded workbook and return (total_rows, unique_creators) (spec §13).

    Checks are ordered cheapest-first: extension → size → real XLSX/ZIP structure → readable
    workbook → creator column present → at least one unique creator. Every failure raises
    ``UploadValidationError`` with a stable code the API surfaces as JSON.
    """
    import zipfile

    input_file = store.input_path(job_id)
    path = str(input_file)

    if not path.lower().endswith(".xlsx"):
        raise UploadValidationError(
            ".xlsx 형식의 Excel 파일만 업로드할 수 있습니다.", "INVALID_FILE_TYPE"
        )

    if input_file.stat().st_size > _max_upload_bytes():
        raise UploadValidationError(
            "업로드 파일 크기가 허용 범위를 초과했습니다.", "FILE_TOO_LARGE"
        )

    if not zipfile.is_zipfile(path):
        # A .xlsx is a ZIP container; a non-ZIP file is corrupt or mislabeled.
        raise UploadValidationError(
            "Excel 파일을 열 수 없습니다. 파일이 손상되었거나 지원되지 않는 형식입니다.",
            "INVALID_EXCEL",
        )

    try:
        plan = read_workbook(path)
    except Exception as exc:  # noqa: BLE001 — encrypted/corrupt workbooks land here
        raise UploadValidationError(
            "Excel 파일을 열 수 없습니다. 파일이 손상되었거나 지원되지 않는 형식입니다.",
            "INVALID_EXCEL",
        ) from exc

    if not plan.sheets:
        raise UploadValidationError(
            "크리에이터 열을 찾을 수 없습니다. Creator Name 열이 포함된 템플릿을 사용하세요.",
            "CREATOR_COLUMN_NOT_FOUND",
        )

    if not plan.unique_usernames():
        raise UploadValidationError(
            "조회할 크리에이터가 없습니다. Creator Name 열의 두 번째 행부터 username을 입력하세요.",
            "NO_CREATORS",
        )

    return plan.total_rows, len(plan.unique_usernames())

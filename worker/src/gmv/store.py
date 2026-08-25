"""Local filesystem job store (spec §13, §14, §17).

Persists job metadata + per-row results as JSON on disk so state survives a Worker restart
(spec §17 recovery). Input/output Excel files live alongside. This is the Worker's own
durable store; the web app mirrors high-level state into Supabase (see supabase/).

Files never logged; stored under a gitignored ``storage/`` root (spec §14).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from gmv.models import RowStatus


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class JobStatus:
    UPLOADED = "uploaded"
    VALIDATING = "validating"
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    MERGING = "merging"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"
    NEEDS_LOGIN = "needs_login"
    PAUSED = "paused"


class RowRecord(BaseModel):
    sheet_name: str
    excel_row_number: int
    creator: str
    normalized_username: str | None = None
    account_code: str | None = None
    status: str = RowStatus.PENDING.value
    raw_gmv: str | None = None
    gmv_value: float | None = None
    gmv_type: str | None = None
    items_sold: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    current_stage: str | None = None
    source: str | None = None
    display_name: str | None = None
    pps: str | None = None
    category: str | None = None
    audience: str | None = None
    status_badge: str | None = None
    items_sold_raw: str | None = None


class InvitationAcceptRecord(BaseModel):
    """Durable state for one requested Target Invitation, in original input order."""

    order: int
    invitation_name: str
    owner: str
    product: str
    date: str
    number: str
    market: str
    status: str = "QUEUED"
    message: str | None = None
    processed_at: str | None = None
    invited_count: int = 0
    added_products_count: int = 0
    posted_content_count: int = 0


class InvitationCreatorRecord(BaseModel):
    """One creator row extracted from an invitation's Creator details dialog."""

    order: int
    keyword: str
    invitation_name: str
    creator: str
    nickname: str | None = None
    creator_id: str | None = None
    region: str | None = None
    market: str
    added_products: bool = False
    posted_content: bool = False


class JobLogRecord(BaseModel):
    time: str
    message: str
    status: str = "INFO"


class JobRecord(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    original_filename: str
    selected_profile_code: str | None = None
    concurrency: int = 1
    status: str = JobStatus.QUEUED
    total_rows: int = 0
    unique_creators: int = 0
    processed: int = 0
    success: int = 0
    range_rows: int = 0
    failed: int = 0
    current: str | None = None
    output_filename: str | None = None
    created_at: str = Field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    rows: list[RowRecord] = Field(default_factory=list)
    # Optional fields used by Invitation Acceptor jobs. Defaults keep all legacy GMV JSON valid.
    job_type: str = "GMV"
    unique_invitations: int = 0
    invitation_accept_states: list[InvitationAcceptRecord] = Field(default_factory=list)
    invitation_creator_rows: list[InvitationCreatorRecord] = Field(default_factory=list)
    logs: list[JobLogRecord] = Field(default_factory=list)
    current_phase: str | None = None
    search_page: int = 0
    search_total_pages: int = 0


class JobStore:
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, job_id: str) -> Path:
        d = self.root / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def input_path(self, job_id: str) -> Path:
        return self._dir(job_id) / "input.xlsx"

    def input_text_path(self, job_id: str) -> Path:
        return self._dir(job_id) / "input.txt"

    def output_path(self, job_id: str) -> Path:
        return self._dir(job_id) / "output.xlsx"

    def _json_path(self, job_id: str) -> Path:
        return self._dir(job_id) / "job.json"

    def save(self, record: JobRecord) -> JobRecord:
        self._json_path(record.id).write_text(
            record.model_dump_json(indent=2), encoding="utf-8"
        )
        return record

    def get(self, job_id: str) -> JobRecord | None:
        path = self._json_path(job_id)
        if not path.exists():
            return None
        return JobRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def delete(self, job_id: str) -> None:
        """Remove a job's directory and all its files (failed-upload cleanup, spec §13)."""
        import shutil

        d = self.root / job_id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    def list(self) -> list[JobRecord]:
        out: list[JobRecord] = []
        for d in sorted(self.root.iterdir()):
            jp = d / "job.json"
            if jp.exists():
                out.append(JobRecord.model_validate_json(jp.read_text(encoding="utf-8")))
        return sorted(out, key=lambda r: r.created_at, reverse=True)

"""Shared pydantic models and enums (spec §5, §15)."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel


class GmvValueType(str, Enum):
    """Classification of a parsed GMV value (spec §3, §4)."""

    EXACT = "EXACT"
    RANGE_MAX = "RANGE_MAX"
    OPEN_ENDED_ESTIMATE = "OPEN_ENDED_ESTIMATE"
    NOT_FOUND = "NOT_FOUND"
    ERROR = "ERROR"


class ParsedGmv(BaseModel):
    """Result of parsing a raw GMV string from the Affiliate Center (spec §5)."""

    value: Decimal | None = None
    raw_value: str | None = None
    value_type: GmvValueType
    lower_bound: Decimal | None = None
    upper_bound: Decimal | None = None

    model_config = {"arbitrary_types_allowed": True}


class JobCancelledError(Exception):
    """Raised inside the processing loop when a cancel was requested (cooperative stop)."""


class RowStatus(str, Enum):
    """Per-row processing status (spec §13, §15)."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    RANGE = "RANGE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ErrorCode(str, Enum):
    """Row-level error codes (spec §15)."""

    CREATOR_NOT_FOUND = "CREATOR_NOT_FOUND"
    CREATOR_MISMATCH = "CREATOR_MISMATCH"
    RESULT_USERNAME_NOT_FOUND = "RESULT_USERNAME_NOT_FOUND"
    RESULT_ROW_NOT_FOUND = "RESULT_ROW_NOT_FOUND"
    AUTOCOMPLETE_NOT_FOUND = "AUTOCOMPLETE_NOT_FOUND"
    AUTOCOMPLETE_USERNAME_NOT_FOUND = "AUTOCOMPLETE_USERNAME_NOT_FOUND"
    AUTOCOMPLETE_MISMATCH = "AUTOCOMPLETE_MISMATCH"
    AUTOCOMPLETE_CLICK_FAILED = "AUTOCOMPLETE_CLICK_FAILED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    GMV_NOT_FOUND = "GMV_NOT_FOUND"
    GMV_VALUE_NOT_FOUND = "GMV_VALUE_NOT_FOUND"
    GMV_PARSE_ERROR = "GMV_PARSE_ERROR"
    CREATOR_OPEN_FAILED = "CREATOR_OPEN_FAILED"
    CREATOR_DETAIL_TIMEOUT = "CREATOR_DETAIL_TIMEOUT"
    SELECTOR_ERROR = "SELECTOR_ERROR"
    SEARCH_TIMEOUT = "SEARCH_TIMEOUT"
    SEARCH_PAGE_NOT_FOUND = "SEARCH_PAGE_NOT_FOUND"
    AFFILIATE_PAGE_NOT_READY = "AFFILIATE_PAGE_NOT_READY"
    AFFILIATE_ENTRY_NOT_READY = "AFFILIATE_ENTRY_NOT_READY"
    RATE_LIMITED = "RATE_LIMITED"
    BROWSER_ERROR = "BROWSER_ERROR"
    PARSE_ERROR = "PARSE_ERROR"


class LookupResult(BaseModel):
    """The outcome of looking up one unique creator, keyed by normalized username.

    Applied to every Excel row that shares the same normalized username (spec §2 dedupe).
    """

    normalized_username: str
    gmv: ParsedGmv | None = None
    items_sold: int | None = None
    status: RowStatus = RowStatus.PENDING
    account_label: str | None = None
    queried_at: str | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    current_stage: str | None = None  # last pipeline stage reached (SEARCH_INPUT, EXTRACT_GMV, …)
    # Where the GMV came from + best-effort creator profile fields read from the result row.
    source: str | None = None
    display_name: str | None = None
    pps: str | None = None
    category: str | None = None
    audience: str | None = None
    status_badge: str | None = None
    items_sold_raw: str | None = None

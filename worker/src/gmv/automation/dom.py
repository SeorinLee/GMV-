"""Pure DOM-text parsing helpers (spec §11).

The legacy scripts read a creator result block's ``inner_text`` and took the first ``$``
line as GMV and the following digit line as items-sold. That heuristic is reused here as a
DOM fallback, hardened and made pure so it is unit-testable without a browser.

``result_signature`` lets the driver confirm the result region actually changed between
searches, preventing a stale previous result from being saved for the next creator
(spec §11 — ``previous_result_signature``).
"""

from __future__ import annotations

import re

_INT_RE = re.compile(r"^\d[\d,]*$")


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def extract_from_row_text(text: str) -> tuple[str | None, int | None]:
    """Return ``(raw_gmv_string, items_sold)`` from a result block's text.

    GMV = first line containing ``$`` (kept verbatim so the GMV parser can handle ranges).
    Items sold = the first subsequent integer-only line, if any.
    """
    lines = _lines(text)
    gmv_raw: str | None = None
    items_sold: int | None = None

    for i, line in enumerate(lines):
        if "$" in line:
            gmv_raw = line
            for follow in lines[i + 1 :]:
                if _INT_RE.match(follow):
                    items_sold = int(follow.replace(",", ""))
                    break
            break

    return gmv_raw, items_sold


def result_signature(text: str) -> str:
    """Stable signature of a result block, used to detect that the result changed."""
    return "\n".join(_lines(text))

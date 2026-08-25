"""GMV extraction providers with priority Network -> DOM (spec §11, D9).

Only responses the normal page already receives are inspected — no auth bypass, no
private-API guessing (spec §11). If network extraction yields nothing, the driver falls
back to DOM text. Both return a raw GMV string + optional items-sold; parsing/typing is
done by :func:`gmv.gmv_parser.parse_gmv` so range/open-ended rules apply uniformly.
"""

from __future__ import annotations

import re
from typing import Any

# Candidate keys seen in creator/analytics JSON payloads. Best-effort; unknown shapes just
# fall through to the DOM extractor.
_GMV_KEYS = ("gmv", "gmv_amount", "gmvAmount", "total_gmv", "totalGmv", "gmv_str", "gmvStr")
_USERNAME_KEYS = ("unique_id", "uniqueId", "username", "handle", "creator_unique_id")
_ITEMS_KEYS = ("items_sold", "itemsSold", "sold_count", "soldCount", "sku_sold")
_COUNT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*[kmb]?", re.IGNORECASE)
_COUNT_UNITS = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _walk(obj: Any):
    """Yield every dict in a nested JSON structure."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _first_key(d: dict, keys: tuple[str, ...]) -> Any | None:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _items_count(value: Any) -> int | None:
    """Expand numeric/abbreviated item counts found in JSON (``4.7K`` -> ``4700``)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    values: list[float] = []
    for match in _COUNT_RE.finditer(str(value)):
        token = match.group(0).replace(",", "").replace(" ", "")
        unit = token[-1].lower() if token[-1:].lower() in _COUNT_UNITS else ""
        number = token[:-1] if unit else token
        try:
            values.append(float(number) * _COUNT_UNITS[unit])
        except ValueError:
            continue
    return int(max(values)) if values else None


def extract_from_json(
    payload: Any,
    expected_username_norm: str | None,
    *,
    require_username: bool = False,
) -> tuple[str | None, int | None]:
    """Find a GMV string (and items) for the expected creator in a JSON payload.

    Only returns a value from a dict that also carries a matching username, so we never
    attribute another creator's GMV (spec §16). If no username field is present anywhere,
    returns the first GMV found (single-result search responses).
    """
    from gmv.creator import normalize_username

    fallback: tuple[str | None, int | None] = (None, None)
    saw_username_field = False

    for d in _walk(payload):
        gmv = _first_key(d, _GMV_KEYS)
        if gmv is None:
            continue
        gmv_str = str(gmv)
        items_raw = _first_key(d, _ITEMS_KEYS)
        items = _items_count(items_raw)

        uname = _first_key(d, _USERNAME_KEYS)
        if uname is not None:
            saw_username_field = True
            if expected_username_norm and normalize_username(str(uname)) == expected_username_norm:
                return gmv_str, items
        elif fallback == (None, None):
            fallback = (gmv_str, items)

    # Live browser automation uses strict mode: only an exact username-bearing object may
    # short-circuit the DOM.  The non-strict fallback is kept for callers that already know a
    # payload is the response to one single-result request.
    if require_username:
        return (None, None)
    # If username fields existed but none matched, do not guess.
    return (None, None) if saw_username_field else fallback

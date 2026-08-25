"""GMV string parser (spec §4, §5).

Pure, dependency-light module (no Playwright / UI imports) so it can be unit tested and
reused everywhere. All money math uses ``decimal.Decimal`` — never ``float`` (D1).

Rules:
- Single amount            -> EXACT
- Range (``a-b``, ``a to b``) -> RANGE_MAX (value = max)                        (spec §4)
- "Under/Below/Up to/<"    -> RANGE_MAX (lower 0, upper = amount)               (spec §4)
- "Over/Above/More than/+" -> OPEN_ENDED_ESTIMATE (value = threshold)           (spec §4)
- empty / None / N/A       -> NOT_FOUND
- unparseable non-empty    -> ERROR
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from gmv.models import GmvValueType, ParsedGmv

_UNITS: dict[str, Decimal] = {
    "k": Decimal(1_000),
    "m": Decimal(1_000_000),
    "b": Decimal(1_000_000_000),
}

# Tokens that mean "no value present" rather than "bad value".
_NOT_FOUND_TOKENS = {"", "n/a", "na", "n.a.", "none", "null", "-", "--", "—", "–", "."}

# A number optionally followed by a K/M/B unit. Requires a leading digit so unit letters
# inside words ("Below", "More") are never mistaken for amounts.
_AMOUNT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*([kmb])?", re.IGNORECASE)

_OPEN_ENDED_KEYWORDS = ("over", "above", "more than", "greater than", "at least")
_CAPPED_KEYWORDS = ("under", "below", "less than", "up to", "at most")


def _tidy(value: Decimal) -> Decimal:
    """Drop meaningless trailing zeros (853400.0 -> 853400) while keeping real decimals."""
    if value == value.to_integral_value():
        return value.to_integral_value()
    return value.normalize()


def _parse_amount(number: str, unit: str) -> Decimal:
    amount = Decimal(number.replace(",", ""))
    if unit:
        amount *= _UNITS[unit.lower()]
    return _tidy(amount)


def _extract_amounts(text: str) -> list[Decimal]:
    amounts: list[Decimal] = []
    for match in _AMOUNT_RE.finditer(text):
        try:
            amounts.append(_parse_amount(match.group(1), match.group(2) or ""))
        except (InvalidOperation, KeyError):
            continue
    return amounts


def parse_gmv(raw: str | None) -> ParsedGmv:
    """Parse a raw GMV string into a :class:`ParsedGmv` (spec §5)."""
    if raw is None:
        return ParsedGmv(value=None, raw_value=None, value_type=GmvValueType.NOT_FOUND)

    raw_value = str(raw)
    text = raw_value.strip()

    if text.lower() in _NOT_FOUND_TOKENS:
        return ParsedGmv(value=None, raw_value=raw_value, value_type=GmvValueType.NOT_FOUND)

    low = text.lower()
    is_open_ended = (
        bool(re.search(r"\+\s*$", text))
        or low.startswith((">", "≥"))
        or any(kw in low for kw in _OPEN_ENDED_KEYWORDS)
    )
    is_capped = (
        low.startswith(("<", "≤"))
        or any(kw in low for kw in _CAPPED_KEYWORDS)
    )

    amounts = _extract_amounts(text)

    if not amounts:
        # Non-empty, not a "no value" token, but nothing numeric -> genuinely bad input.
        return ParsedGmv(value=None, raw_value=raw_value, value_type=GmvValueType.ERROR)

    highest = max(amounts)
    lowest = min(amounts)

    if is_open_ended:
        # $5K+ / Over $5K -> use threshold, but never claim EXACT (spec §4, D3).
        return ParsedGmv(
            value=highest,
            raw_value=raw_value,
            value_type=GmvValueType.OPEN_ENDED_ESTIMATE,
            lower_bound=highest,
            upper_bound=None,
        )

    if is_capped:
        # Under $5K / Up to $5K -> finite max (spec §4).
        return ParsedGmv(
            value=highest,
            raw_value=raw_value,
            value_type=GmvValueType.RANGE_MAX,
            lower_bound=lowest if len(amounts) > 1 else Decimal(0),
            upper_bound=highest,
        )

    if len(amounts) >= 2:
        # $1K-$5K / $0 - $5,000 / 0 to 5K -> take the max (spec §4, D2).
        return ParsedGmv(
            value=highest,
            raw_value=raw_value,
            value_type=GmvValueType.RANGE_MAX,
            lower_bound=lowest,
            upper_bound=highest,
        )

    return ParsedGmv(
        value=highest,
        raw_value=raw_value,
        value_type=GmvValueType.EXACT,
    )

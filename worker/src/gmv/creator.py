"""Creator username normalization and matching (spec §2, §16).

TikTok usernames may arrive as ``@handle``, a bare handle, or a profile URL. We normalize
to a canonical lowercase handle. Dots and underscores are preserved (D5) because they are
valid, meaningful characters in real handles (``wendy.sepulveda79``, ``ms.lovely.ivonne_s``).

Matching is exact on the normalized form so ``creatorname`` never matches ``creatorname1``
or ``creator_name`` (spec §16), preventing wrong-creator GMV writes (CREATOR_MISMATCH).
"""

from __future__ import annotations

import re

# Capture the handle out of any tiktok.com URL: tiktok.com/@handle, with optional
# leading protocol/subdomain and trailing slash / query / fragment.
_TIKTOK_URL_RE = re.compile(r"tiktok\.com/@?([^/?#\s]+)", re.IGNORECASE)


def normalize_username(raw: str | None) -> str | None:
    """Return the canonical lowercase handle, or ``None`` if there is no usable value."""
    if raw is None:
        return None

    text = str(raw).strip()
    if not text or text.lower() == "nan":
        return None

    url_match = _TIKTOK_URL_RE.search(text)
    if url_match:
        handle = url_match.group(1)
    else:
        handle = text

    handle = handle.strip().lstrip("@").strip().rstrip("/").strip()
    handle = handle.lower()

    return handle or None


def usernames_match(requested: str | None, found: str | None) -> bool:
    """True only if both normalize to the same non-empty handle (spec §16)."""
    a = normalize_username(requested)
    b = normalize_username(found)
    return a is not None and a == b

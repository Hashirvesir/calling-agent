"""Phone number normalization for Pakistani numbers.

Callers, Telnyx, and spoken input use inconsistent formats:
    +923244283400   E.164 (what Telnyx sends and what we store)
    923244283400    E.164 without the plus
    03244283400     local Pakistani format (11 digits, leading 0)
    3244283400      bare national number (10 digits)

`normalize_phone` collapses any of these to canonical E.164.
`phone_variants` returns every plausible stored representation so a DB
lookup matches regardless of which format a row happens to hold.
"""

from __future__ import annotations

import re

_PK_COUNTRY_CODE = "92"
# Pakistani mobile national numbers are 10 digits after the country code
# (e.g. 3244283400). Landlines vary but mobiles dominate this product.
_NATIONAL_LEN = 10


def _digits_only(raw: str) -> str:
    """Strip everything except digits (drops +, spaces, dashes, parentheses)."""
    return re.sub(r"\D", "", raw or "")


def normalize_phone(raw: str | None) -> str | None:
    """Return canonical E.164 (+92XXXXXXXXXX) for a Pakistani number.

    Returns the digit-normalized "+<digits>" for non-PK or unrecognized
    shapes so international numbers still pass through. Returns None for
    empty/garbage input.
    """
    if not raw:
        return None

    s = (raw or "").strip()
    digits = _digits_only(s)
    if not digits:
        return None

    # 00 international prefix → drop it
    if digits.startswith("00"):
        digits = digits[2:]

    # Already has country code: 92XXXXXXXXXX
    if digits.startswith(_PK_COUNTRY_CODE) and len(digits) == len(_PK_COUNTRY_CODE) + _NATIONAL_LEN:
        return "+" + digits

    # Local format: 0XXXXXXXXXX (leading 0 + 10 national digits)
    if digits.startswith("0") and len(digits) == _NATIONAL_LEN + 1:
        return "+" + _PK_COUNTRY_CODE + digits[1:]

    # Bare national number: XXXXXXXXXX (10 digits, no 0, no country code)
    if len(digits) == _NATIONAL_LEN:
        return "+" + _PK_COUNTRY_CODE + digits

    # If the original already started with + treat as international E.164
    if s.startswith("+"):
        return "+" + digits

    # Unknown shape — best effort: prefix + so it's a valid-ish E.164
    return "+" + digits


def phone_variants(raw: str | None) -> list[str]:
    """Return all plausible stored representations of a number for DB matching.

    Covers the canonical E.164, the no-plus form, the local 0-prefixed form,
    and the bare national number — deduplicated, order-stable.
    """
    canonical = normalize_phone(raw)
    if not canonical:
        return []

    variants: list[str] = [canonical]
    no_plus = canonical.lstrip("+")
    variants.append(no_plus)

    if no_plus.startswith(_PK_COUNTRY_CODE):
        national = no_plus[len(_PK_COUNTRY_CODE):]
        variants.append("0" + national)  # local 0-prefixed
        variants.append(national)        # bare national

    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out

"""Quality (condition) and size gating.

- Condition: Vinted's `status` string, ranked best→worst. A configurable floor
  hides anything below it (default "Good" drops "Satisfactory").
- Size: strict, garment-type-aware matching of Vinted's free-text `size_title`
  against a per-(gender, type) allow-list.
"""
from __future__ import annotations

import re

# Vinted UK condition strings, best → worst (verified from live data).
CONDITION_ORDER = [
    "new with tags",
    "new without tags",
    "very good",
    "good",
    "satisfactory",
]
_CONDITION_RANK = {name: len(CONDITION_ORDER) - i for i, name in enumerate(CONDITION_ORDER)}


def condition_rank(condition: str) -> int:
    """Rank a condition (higher = better); unknown/blank ranks 0."""
    return _CONDITION_RANK.get((condition or "").strip().lower(), 0)


def passes_condition(condition: str, floor: str) -> bool:
    """True if `condition` is at least as good as `floor`."""
    floor_rank = _CONDITION_RANK.get((floor or "").strip().lower())
    if floor_rank is None:  # misconfigured floor → don't silently hide everything
        return True
    return condition_rank(condition) >= floor_rank


# -- size matching -----------------------------------------------------------

def _tokens(size: str) -> set[str]:
    """Upper-cased alphanumeric tokens, e.g. '8 (M)' -> {'8', 'M'}."""
    return {t.upper() for t in re.split(r"[^0-9A-Za-z.]+", size or "") if t}


def _waist(size: str) -> str | None:
    """Extract the waist from a trouser size, ignoring the leg/length number.

    Handles 'W32 L34' -> '32', '32/34' -> '32', '32' -> '32', 'M' -> None.
    """
    m = re.search(r"W\s?(\d{2,3})", size or "", re.I)          # W32 L34
    if m:
        return m.group(1)
    m = re.match(r"\s*(\d{2,3})\s*/\s*\d{2,3}\s*$", size or "")  # 32/34
    if m:
        return m.group(1)
    m = re.match(r"\s*(\d{2,3})\s*$", size or "")               # bare 32
    if m:
        return m.group(1)
    return None


def size_matches(garment_type: str, size: str, allowed: list) -> bool:
    """Strict, type-aware membership test of `size` in the `allowed` set."""
    allowed_set = {str(a).upper() for a in allowed}
    if not allowed_set:
        return True  # no restriction configured for this type

    if garment_type == "trousers":
        # Explicit waist notation (men's 'W32 L34', '32', '30/32') -> read the
        # waist only, so the leg length never causes a false match.
        if re.search(r"W\s?\d", size or "", re.I) or re.match(r"\s*\d{2,3}\s*(/\s*\d{2,3})?\s*$", size or ""):
            waist = _waist(size)
            return waist is not None and waist in allowed_set
        # Otherwise (women's dress-style sizes like 'UK 8', '8 (S)') fall back to
        # exact token matching.
        return bool(_tokens(size) & allowed_set)

    # shoes + clothes: match any size-like token exactly (e.g. 'UK 9' -> '9',
    # 'XS', 'M', women's numeric '8'). EU sizes like '39' won't match '9'.
    return bool(_tokens(size) & allowed_set)

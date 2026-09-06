"""Data models for Vinted catalog items, with defensive JSON parsing."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VintedItem:
    id: int
    title: str
    price: float
    currency: str
    brand_title: str
    size: str
    condition: str      # Vinted `status`, e.g. "Very good", "Good", "Satisfactory"
    url: str
    image_url: str

    # -- attention signals (the "hotness" inputs) ---------------------------
    # Vinted omits these for some listings, so they are genuinely optional and
    # must never be coerced to 0 — "nobody has liked this" and "we don't know
    # how many people have liked this" are different facts, and treating the
    # second as the first would silently suppress alerts.
    favourite_count: int | None = None
    view_count: int | None = None
    promoted: bool = False          # paid boost; inflates views, scored separately
    listed_ts: int | None = None    # unix seconds; when the listing went up

    @classmethod
    def from_json(cls, raw: dict, *, base_url: str) -> "VintedItem | None":
        """Build an item from a raw catalog entry.

        Vinted's API is undocumented and its schema drifts, so every field is
        read defensively and anything unparseable returns None (caller skips it).
        """
        try:
            item_id = int(raw["id"])
        except (KeyError, TypeError, ValueError):
            return None

        price = _extract_price(raw.get("price"))
        if price is None:
            return None

        currency = _extract_currency(raw.get("price")) or ""

        url = raw.get("url") or f"{base_url}/items/{item_id}"

        return cls(
            id=item_id,
            title=str(raw.get("title") or "").strip(),
            price=price,
            currency=currency,
            brand_title=str(raw.get("brand_title") or "").strip(),
            size=str(raw.get("size_title") or "").strip(),
            condition=str(raw.get("status") or "").strip(),
            url=url,
            image_url=_extract_photo(raw.get("photo")),
            favourite_count=_extract_count(raw.get("favourite_count")),
            view_count=_extract_count(raw.get("view_count")),
            promoted=bool(raw.get("promoted")),
            listed_ts=_extract_listed_ts(raw.get("photo")),
        )


def _extract_price(price_field) -> float | None:
    """Price is `{"amount": "12.0", "currency_code": "GBP"}` on newer responses,
    a bare number/string on older ones."""
    if price_field is None:
        return None
    if isinstance(price_field, dict):
        amount = price_field.get("amount")
    else:
        amount = price_field
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _extract_currency(price_field) -> str | None:
    if isinstance(price_field, dict):
        code = price_field.get("currency_code")
        return str(code) if code else None
    return None


def _extract_photo(photo_field) -> str:
    if isinstance(photo_field, dict):
        return str(photo_field.get("full_size_url") or photo_field.get("url") or "")
    return ""


def _extract_count(value) -> int | None:
    """Read an attention counter, preserving 'unknown' as None.

    Negative values are treated as unknown rather than clamped: a negative like
    count is nonsense, and silently turning it into 0 would feed a bogus sample
    into the velocity maths.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count >= 0 else None


def _extract_listed_ts(photo_field) -> int | None:
    """When the listing went up, via its main photo's upload timestamp.

    Vinted's catalog response carries no explicit listing time, but the photo is
    uploaded as part of creating the listing, so its timestamp is the closest
    proxy available without a per-item request.
    """
    if not isinstance(photo_field, dict):
        return None
    high_res = photo_field.get("high_resolution")
    if not isinstance(high_res, dict):
        return None
    try:
        timestamp = int(high_res["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    return timestamp if timestamp > 0 else None

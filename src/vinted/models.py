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
    url: str
    image_url: str

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
            url=url,
            image_url=_extract_photo(raw.get("photo")),
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

"""Data models for Vinted catalog items, with defensive JSON parsing."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..filters import condition_rank

log = logging.getLogger(__name__)


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

        url = _extract_url(raw.get("url"), base_url=base_url, item_id=item_id)

        size, condition = _extract_size_and_condition(raw)

        return cls(
            id=item_id,
            title=str(raw.get("title") or "").strip(),
            price=price,
            currency=currency,
            brand_title=_extract_brand(raw),
            size=size,
            condition=condition,
            url=url,
            image_url=_extract_photo(raw.get("photo")),
            favourite_count=_extract_count(raw.get("favourite_count")),
            view_count=_extract_count(raw.get("view_count")),
            promoted=bool(raw.get("promoted")),
            listed_ts=_extract_listed_ts(raw.get("photo")),
        )


def _extract_brand(raw: dict) -> str:
    """Brand, from wherever this version of the response keeps it.

    The catalogue service returns `brand_title` empty and puts the brand in
    `item_box.first_line` instead -- measured at 0% and 100% coverage
    respectively. This is not cosmetic: `scrape()` maps the brand back to a
    config entry and silently skips anything it cannot match, so reading the
    wrong field drops every listing and looks like a quiet market.
    """
    direct = str(raw.get("brand_title") or "").strip()
    if direct:
        return direct
    box = raw.get("item_box")
    if isinstance(box, dict):
        return str(box.get("first_line") or "").strip()
    return ""


def _extract_size_and_condition(raw: dict) -> tuple[str, str]:
    """Size and condition, which now arrive joined in one display string.

    `size_title` and `status` used to carry them and are both absent from the
    catalogue service. What it sends instead is `item_box.second_line`, a
    human-facing string like "M · Very good".

    The parts are classified by *content*, not position: anything matching a
    known Vinted condition is the condition, anything else is the size. Relying
    on order would break the first time a listing has only one of the two, which
    is common -- plenty of listings have a condition and no size.
    """
    size = str(raw.get("size_title") or "").strip()
    condition = str(raw.get("status") or "").strip()
    if size and condition:
        return size, condition

    box = raw.get("item_box")
    second = str(box.get("second_line") or "").strip() if isinstance(box, dict) else ""
    parts = [p.strip() for p in second.split("·") if p.strip()]
    for part in parts:
        if condition_rank(part):
            condition = condition or part
        else:
            size = size or part

    # Classifying by content means an unrecognised vocabulary is indistinguishable
    # from no condition at all -- and a blank condition fails `passes_condition`,
    # so every listing is dropped downstream with nothing to say why. That is
    # exactly how a locale slip would present: silently, as an empty market.
    if parts and not condition:
        log.warning(
            "No recognised condition in %r -- is the response in the expected "
            "language? Every listing will be filtered out until it is.", second,
        )
    return size, condition


def _extract_url(raw_url, *, base_url: str, item_id: int) -> str:
    """The listing's address, always absolute.

    The catalogue service returns a *path* -- "/items/10012275111-arcteryx" --
    where the old endpoint returned a full URL. Nothing downstream tolerates
    that. The page whitelists hrefs to http(s) before assigning them, correctly,
    so a relative path silently produces a card with no link at all: not a broken
    link, an inert one. Alerts would carry the same unusable address.

    Resolving it here rather than in the page fixes the feed, the database, the
    fallback and the push notification from one place -- and leaves the
    whitelist, which is an XSS guard, alone.
    """
    path = str(raw_url or "").strip()
    if not path:
        return f"{base_url}/items/{item_id}"
    if path.startswith(("http://", "https://")):
        return path
    return f"{base_url}/{path.lstrip('/')}"


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

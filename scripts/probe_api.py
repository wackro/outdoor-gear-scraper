"""Verify the assumptions the hotness poller is built on, against the live API.

Run this FIRST, before trusting anything the poller reports:

    python -m scripts.probe_api

It answers three questions, and the whole hot path depends on the answers:

  1. Does a catalog item carry `favourite_count` and `view_count`? These are the
     hotness signal. Without them there is nothing to measure.
  2. Does a *catalog-less* query work — `brand_ids` with no `catalog_ids`? This
     is the "firehose" that makes 60-second polling cheap (one request for the
     newest items across every category, instead of one request per category).
  3. Does `photo.high_resolution.timestamp` give a usable listing time? It is
     how we know a listing's true age rather than just when we first saw it.

Exits non-zero if a hard requirement is missing, so it can gate CI.
"""
from __future__ import annotations

import json
import logging
import sys

from src.config import load_config
from src.vinted.brand_resolver import resolve_brands
from src.vinted.client import CATALOG_PATH, VintedClient, VintedError

log = logging.getLogger("probe")

# Fields the hot path reads. Missing "required" fields means the design is dead.
REQUIRED = ("favourite_count",)
NICE_TO_HAVE = ("view_count", "promoted", "is_visible", "total_item_price")


def _pct_present(raws: list[dict], field: str) -> float:
    """Share of items where `field` is present AND not null.

    Vinted omits these counts for some listings, so "present on one item" is not
    good enough to build on — we want to know how often we can rely on it.
    """
    if not raws:
        return 0.0
    return 100.0 * sum(1 for r in raws if r.get(field) is not None) / len(raws)


def probe_firehose(client: VintedClient, brand_ids: list[int]) -> list[dict]:
    """Fetch the newest items for our brands across ALL categories in one call."""
    return client._request(
        {
            "page": 1,
            "per_page": 96,
            "order": "newest_first",
            "brand_ids": ",".join(str(b) for b in brand_ids),
            "currency": client.config.currency,
        },
        path=CATALOG_PATH,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config()
    client = VintedClient(config)

    brands = resolve_brands(client, config.brands)
    brand_ids = list(brands.ids.values())
    print(f"Resolved {len(brand_ids)} brand ids.\n")

    # -- Q2: does the catalog-less firehose work? ---------------------------
    print("=" * 70)
    print("Q2. Catalog-less firehose (brand_ids only, no catalog_ids)")
    print("=" * 70)
    try:
        raws = probe_firehose(client, brand_ids)
    except VintedError as exc:
        print(f"  FAIL: {exc}")
        print("  -> Fall back to the per-category sweep (3x the request cost).")
        return 2
    if not raws:
        print("  FAIL: query accepted but returned no items.")
        return 2
    print(f"  OK: returned {len(raws)} items in ONE request.\n")

    # -- Q1: are the hotness counters there? --------------------------------
    print("=" * 70)
    print("Q1. Attention counters on catalog items")
    print("=" * 70)
    missing_required = []
    for field in REQUIRED + NICE_TO_HAVE:
        pct = _pct_present(raws, field)
        tag = "required" if field in REQUIRED else "optional"
        print(f"  {field:<20} present on {pct:5.1f}% of items   ({tag})")
        if field in REQUIRED and pct == 0.0:
            missing_required.append(field)
    print()

    # -- Q3: is there a real listing timestamp? -----------------------------
    print("=" * 70)
    print("Q3. Listing timestamp (photo.high_resolution.timestamp)")
    print("=" * 70)
    stamped = 0
    for raw in raws:
        photo = raw.get("photo")
        if isinstance(photo, dict):
            hi = photo.get("high_resolution")
            if isinstance(hi, dict) and hi.get("timestamp"):
                stamped += 1
    print(f"  present on {100.0 * stamped / len(raws):5.1f}% of items\n")

    # -- a couple of real items, so schema drift is visible at a glance -----
    print("=" * 70)
    print("Sample item (raw JSON, truncated)")
    print("=" * 70)
    print(json.dumps(raws[0], indent=1)[:2500])
    print()
    print("Top-level keys:", sorted(raws[0].keys()))

    if missing_required:
        print(f"\nFAIL: {', '.join(missing_required)} absent from every item.")
        print("Hotness is not observable from this endpoint — STOP and rethink.")
        return 1

    print("\nAll hard requirements met.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

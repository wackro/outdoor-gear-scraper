"""Deal detection: flag active items priced well below their baseline."""
from __future__ import annotations

import sqlite3

from ..config import Config
from .baseline import BaselineProvider

EPS = 1e-6


def detect_deals(
    items: list[sqlite3.Row],
    provider: BaselineProvider,
    config: Config,
) -> list[dict]:
    """Return deal rows (dicts ready for Database.replace_deals).

    An item is a deal when its price is at least `threshold` below the reference
    price for its brand+category. Brackets without a reference (too little price
    history and no RRP) are skipped, so a cold start yields no false bargains.
    """
    deals: list[dict] = []
    for item in items:
        brand = item["brand"]
        category = item["category"]
        price = item["price"]

        ref = provider.reference(brand, category)
        if ref is None or ref.value <= 0:
            continue

        threshold = config.threshold_for(brand)
        if price > ref.value * (1 - threshold):
            continue

        discount_pct = 1 - price / ref.value
        if ref.spread is not None:
            # Robust z-score: how many MADs below the reference this price sits.
            deal_score = (ref.value - price) / (1.4826 * ref.spread + EPS)
        else:
            # No spread available (e.g. RRP) — use the discount as the score proxy.
            deal_score = discount_pct

        deals.append(
            {
                "item_id": item["id"],
                "brand": brand,
                "category": category,
                "price": price,
                "baseline": round(ref.value, 2),
                "baseline_src": ref.source,
                "discount_pct": round(discount_pct, 4),
                "deal_score": round(deal_score, 3),
            }
        )
    return deals

"""Render the site with realistic sample data, for reviewing the design.

The page normally gets its data from a live poller. This builds an equivalent
feed from listings already in the database — real brands, titles, prices, photos
and sizes — with plausible attention figures layered on, so the layout can be
judged without waiting for a poller run.

    python -m scripts.preview_site [output.html]

The result is self-contained (the feed is inlined), so it can be opened straight
off disk or sent to a phone.
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

from src.config import load_config
from src.site.generator import _section, render_site
from src.storage.db import DEFAULT_DB_PATH, Database

SAMPLE_SIZE = 48


def sample_feed(db: Database, *, seed: int = 7) -> dict:
    """Build a feed from real listings with synthesised attention numbers."""
    rng = random.Random(seed)
    rows = db.conn.execute(
        """
        SELECT d.item_id, d.price, d.baseline, d.discount_pct,
               i.title, i.brand, i.brand_title, i.gender, i.garment_type,
               i.size, i.condition, i.url, i.image_url, i.currency
        FROM deals d JOIN items i ON i.id = d.item_id
        WHERE i.active = 1 AND i.image_url != '' AND d.price >= 15
        ORDER BY d.discount_pct DESC LIMIT ?
        """,
        (SAMPLE_SIZE * 3,),
    ).fetchall()
    rows = rng.sample(list(rows), min(SAMPLE_SIZE, len(rows)))

    items, sell_times = [], []
    total = len(rows)
    for index, row in enumerate(rows):
        # Heat assigned by rank so the preview always shows the full range --
        # a few very hot at the top, a long lukewarm tail below. Random draws
        # left whole sections with no alerted or sold cards to look at.
        heat = round(0.97 - (index / max(total - 1, 1)) * 0.86
                     + rng.uniform(-0.03, 0.03), 3)
        heat = max(0.05, min(0.99, heat))
        fav_per_hour = round(heat * rng.uniform(30, 140), 1)
        age = round(rng.uniform(4, 175), 1)
        favourites = max(0, int(fav_per_hour * age / 60))
        alerted = heat > 0.70
        sold = alerted and rng.random() < 0.5
        seconds = int(age * 60 * rng.uniform(0.35, 0.9)) if sold else None
        if seconds:
            sell_times.append(seconds)
        items.append({
            "id": row["item_id"],
            "title": row["title"] or "Untitled listing",
            "brand": row["brand"],
            "brand_title": row["brand_title"] or "",
            "gender": row["gender"] or "men",
            "type": row["garment_type"] or "clothes",
            "section": _section(row["gender"] or "men", row["garment_type"] or "clothes"),
            "price": row["price"],
            "currency": row["currency"] or "GBP",
            "size": row["size"] or "",
            "condition": row["condition"] or "",
            "url": row["url"] or "",
            "image_url": row["image_url"] or "",
            "heat": heat,
            "favourites": favourites,
            "views": favourites * rng.randint(8, 20),
            "fav_per_hour": fav_per_hour,
            "view_per_hour": round(fav_per_hour * rng.uniform(9, 18), 1),
            "age_minutes": age,
            "measured": rng.random() > 0.12,
            "baseline": row["baseline"],
            "discount_pct": row["discount_pct"],
            "alerted": alerted,
            "sold": sold,
            "seconds_to_sell": seconds,
        })

    items.sort(key=lambda i: i["heat"], reverse=True)
    sell_times.sort()
    return {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "bar": {"favourites_per_hour": 12.4, "adaptive": True, "sample_size": 1840},
        "counts": {
            "tracked": len(items),
            "alerted": sum(1 for i in items if i["alerted"]),
            "sold": sum(1 for i in items if i["sold"]),
        },
        "median_seconds_to_sell": sell_times[len(sell_times) // 2] if sell_times else None,
        "items": items,
    }


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else Path("preview/index.html")
    config = load_config()
    # Read-only: opening the committed database normally would run the additive
    # migration and rewrite 72MB of binary into the working tree, just to draw a
    # preview.
    with Database(f"file:{DEFAULT_DB_PATH}?mode=ro&immutable=1") as db:
        feed = sample_feed(db)
        # Swap the DB-derived fallback for our sample by patching the builder the
        # renderer calls — keeps the preview honest, using the real render path.
        import src.site.generator as generator
        generator.build_fallback_feed = lambda _db, **_kw: feed
        render_site(db, currency=config.currency, output_dir=out.parent,
                    refresh_sec=config.site.refresh_sec)
    print(f"Wrote {out} ({out.stat().st_size // 1024}KB, {len(feed['items'])} listings)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

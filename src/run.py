"""Orchestrator: scrape → store → baseline → detect → render.

Run with `python -m src.run`. Exit codes:
  0  success (site regenerated)
  1  scrape produced no items at all (likely blocked) — DB left untouched
  2  unexpected fatal error
"""
from __future__ import annotations

import logging
import sys

from .config import Config, load_config
from .pricing.baseline import (
    CompositeBaseline,
    HistoryBaseline,
    RRPBaseline,
    compute_baselines,
)
from .pricing.deals import detect_deals
from .site.generator import render_site
from .storage.db import Database
from .vinted.brand_resolver import BrandResolution, resolve_brands
from .vinted.category_resolver import resolve_category_ids
from .vinted.client import VintedClient, VintedError, _norm

log = logging.getLogger("run")


def scrape(
    config: Config,
    db: Database,
    client: VintedClient,
    brands: BrandResolution,
    category_ids: dict[str, int],
) -> int:
    """Scrape every category once (filtered to all watched brands at once).

    One request per category returns the newest items across every brand; each
    item is mapped back to its brand via `brand_title`. Failures are isolated per
    category. Returns the total number of items stored.
    """
    all_brand_ids = list(brands.ids.values())
    if not all_brand_ids:
        return 0

    total = 0
    for category in config.categories:
        catalog_id = category_ids.get(category.name)
        if catalog_id is None:
            continue  # unresolved category (already logged)
        try:
            items = client.fetch_items(all_brand_ids, catalog_id)
        except VintedError as exc:
            log.error("Scrape failed for %s: %s", category.name, exc)
            continue
        stored = 0
        for item in items:
            brand_name = brands.name_by_title.get(_norm(item.brand_title))
            if brand_name is None:
                continue  # brand not on our watchlist (shouldn't happen given the filter)
            db.upsert_item(
                item, brand=brand_name, category=category.name, catalog_id=catalog_id,
                gender=category.gender, garment_type=category.type,
            )
            db.add_observation(item, brand=brand_name, category=category.name)
            stored += 1
        total += stored
        log.info("%s: %d items", category.name, stored)
        client.throttle()  # polite pause between queries
    return total


def rebuild_deals(config: Config, db: Database) -> int:
    """Recompute baselines and re-detect deals from current data."""
    observations = db.observations_within(config.deals.window_days)
    bracket_stats, brand_stats = compute_baselines(observations)

    db.replace_baselines(
        [(b, c, s.median, s.mad, s.sample_size) for (b, c), s in bracket_stats.items()]
    )

    provider = CompositeBaseline(
        HistoryBaseline(bracket_stats, brand_stats, config.deals.min_samples),
        RRPBaseline(config),
    )
    deals = detect_deals(db.active_items(), provider, config)
    db.replace_deals(deals)
    return len(deals)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = load_config()

    with Database() as db:
        client = VintedClient(config)
        brands = resolve_brands(client, config.brands)
        log.info("Resolved %d/%d brands to ids.", len(brands.ids), len(config.brands))
        category_ids = resolve_category_ids(client, config.categories)
        log.info("Resolved %d/%d categories to ids.", len(category_ids), len(config.categories))
        total = scrape(config, db, client, brands, category_ids)

        if total == 0:
            # Almost certainly blocked or the API changed. Do NOT commit — this
            # keeps the last-good DB and site intact — and fail so CI alerts us.
            log.error("Scrape returned 0 items across all queries; aborting without writing.")
            return 1

        stale = db.mark_stale_items(config.deals.stale_days)
        pruned = db.prune_observations(config.deals.window_days)
        deal_count = rebuild_deals(config, db)
        db.commit()
        log.info(
            "Stored %d items, marked %d stale, pruned %d observations, flagged %d deals.",
            total, stale, pruned, deal_count,
        )

        render_site(db, currency=config.currency)
        log.info("Site rendered to docs/.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — top-level guard so CI gets a clean exit code
        logging.getLogger("run").exception("Fatal error")
        sys.exit(2)

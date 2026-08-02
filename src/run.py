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
from .vinted.brand_resolver import resolve_brand_ids
from .vinted.client import VintedClient, VintedError

log = logging.getLogger("run")


def scrape(config: Config, db: Database, client: VintedClient, brand_ids: dict[str, int]) -> int:
    """Scrape every brand+category, storing items and price observations.

    Failures are isolated per brand+category so one bad query never aborts the
    whole run. Returns the total number of items stored across all queries.
    """
    total = 0
    for brand in config.brands:
        brand_id = brand_ids.get(brand.name)
        if brand_id is None:
            continue  # unresolved brand (already logged by the resolver)
        for category, catalog_id in config.categories.items():
            try:
                items = client.fetch_items(brand_id, catalog_id)
            except VintedError as exc:
                log.error("Scrape failed for %s/%s: %s", brand.name, category, exc)
                continue
            for item in items:
                db.upsert_item(item, brand=brand.name, category=category, catalog_id=catalog_id)
                db.add_observation(item, brand=brand.name, category=category)
            total += len(items)
            log.info("%s / %s: %d items", brand.name, category, len(items))
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
        brand_ids = resolve_brand_ids(client, config.brands)
        log.info("Resolved %d/%d brands to ids.", len(brand_ids), len(config.brands))
        total = scrape(config, db, client, brand_ids)

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

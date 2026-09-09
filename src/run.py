"""Orchestrator: scrape → store → baseline → detect → render.

Run with `python -m src.run`. Exit codes:
  0  success (site regenerated)
  1  scrape produced no items at all (likely blocked) — DB left untouched
  2  unexpected fatal error

`--render-only` skips straight to rendering from the committed database. The page
shell is code, so a change to it should reach the site in a minute; without this
flag the only way to re-render would be to scrape Vinted again, which is both
slow and a pointless request against a rate-limited, undocumented API.
"""
from __future__ import annotations

import argparse
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
from .hot.state import DEFAULT_HOT_DB
from .storage.db import Database
from .vinted.brand_resolver import BrandResolution, resolve_brands
from .vinted.client import VintedClient, VintedError, _norm

log = logging.getLogger("run")


def scrape(
    config: Config,
    db: Database,
    client: VintedClient,
    brands: BrandResolution,
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
        catalog_id = category.id
        try:
            items = client.fetch_items(all_brand_ids, catalog_id)
        except VintedError as exc:
            log.error("Scrape failed for %s: %s", category.label, exc)
            continue
        stored = 0
        for item in items:
            brand_name = brands.name_by_title.get(_norm(item.brand_title))
            if brand_name is None:
                continue  # brand not on our watchlist (shouldn't happen given the filter)
            db.upsert_item(
                item, brand=brand_name, catalog_id=catalog_id,
                gender=category.gender, garment_type=category.type,
            )
            db.add_observation(item, brand=brand_name, catalog_id=catalog_id)
            stored += 1
        total += stored
        log.info("%s: %d items", category.label, stored)
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


def render_only(config: Config) -> int:
    """Regenerate the site from data already in the repo. No network."""
    with Database() as db:
        render_site(
            db, currency=config.currency,
            feed_url=config.site.feed_url,
            feed_branch=config.poll.feed_branch,
            refresh_sec=config.site.refresh_sec,
        )
    log.info("Site rendered to docs/ (render-only; no scrape).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrape Vinted and build the site.")
    parser.add_argument(
        "--render-only", action="store_true",
        help="rebuild the site from the committed database without scraping",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = load_config()

    if args.render_only:
        return render_only(config)

    with Database() as db:
        client = VintedClient(config)
        brands = resolve_brands(client, config.brands)
        log.info("Resolved %d/%d brands to ids.", len(brands.ids), len(config.brands))
        log.info("Watching %d categories.", len(config.categories))
        total = scrape(config, db, client, brands)

        if total == 0:
            # Almost certainly blocked or the API changed. Do NOT commit — this
            # keeps the last-good DB and site intact — and fail so CI alerts us.
            log.error("Scrape returned 0 items across all queries; aborting without writing.")
            return 1

        stale = db.mark_stale_items(config.deals.stale_days)
        pruned = db.prune_observations(config.deals.window_days)
        deal_count = rebuild_deals(config, db)
        # Fold the fast poller's alert log into the committed DB. Its own state
        # lives in a throwaway cache, so this is what makes dedup survive a cache
        # miss instead of re-notifying on everything still listed.
        merged = db.merge_alerts(DEFAULT_HOT_DB)
        # After rebuild_deals, never before: `deals` still holds the previous
        # run's rows until then, and they have a foreign key into `items`.
        # Also after merge_alerts, so items alerted by the poller are protected.
        dead = db.prune_items(config.deals.prune_items_days)
        db.commit()
        reclaimed = db.vacuum() if dead else 0
        log.info(
            "Stored %d items, marked %d stale, pruned %d observations, "
            "flagged %d deals, merged %d alerts, deleted %d dead listings "
            "(reclaimed %.1f MB).",
            total, stale, pruned, deal_count, merged, dead, reclaimed / 1e6,
        )

        render_site(
            db, currency=config.currency,
            feed_url=config.site.feed_url,
            feed_branch=config.poll.feed_branch,
            refresh_sec=config.site.refresh_sec,
        )
        log.info("Site rendered to docs/.")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — top-level guard so CI gets a clean exit code
        logging.getLogger("run").exception("Fatal error")
        sys.exit(2)

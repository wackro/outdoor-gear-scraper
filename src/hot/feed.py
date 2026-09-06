"""The JSON feed behind the website.

The site shows *everything currently being tracked*, ranked by heat — not just
what crossed the alert bar. That is deliberate: seeing the near-misses alongside
the alerts is the only practical way to tell whether the bar is set sensibly. If
the badged listings look right and the top unbadged ones don't, the threshold is
about right; if the reverse, it needs moving. Since the shipped thresholds are
guesses, the page has to double as the instrument for fixing them.

Scoring reuses `velocity()` and `heat()` from `hotness.py` and the same wearable
gates as the alert path, so the site and your phone can never disagree.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timezone

from ..config import Config
from ..filters import passes_condition, size_matches
from .hotness import heat, velocity

log = logging.getLogger(__name__)

FEED_VERSION = 1


def _section(gender: str, garment_type: str) -> str:
    """Bags are unisex and get their own tab; everything else splits by gender."""
    return "bags" if garment_type == "bags" else (gender or "men")


def _entry(meta, vel, score, latest, *, age_minutes, baseline, alerted,
           sold, seconds_to_sell) -> dict:
    price = float(meta["price"])
    discount = None
    if baseline and baseline > 0:
        discount = round(max(0.0, 1 - price / baseline), 4)
    return {
        "id": meta["item_id"],
        "title": meta["title"] or "Untitled listing",
        "brand": meta["brand"] or "",
        # Vinted's own display name, so the page reads "The North Face" rather
        # than the config key "the_north_face".
        "brand_title": meta["brand_title"] or "",
        "gender": meta["gender"] or "",
        "type": meta["garment_type"] or "",
        "section": _section(meta["gender"] or "", meta["garment_type"] or ""),
        "price": price,
        "currency": meta["currency"] or "GBP",
        "size": meta["size"] or "",
        "condition": meta["condition"] or "",
        "url": meta["url"] or "",
        "image_url": meta["image_url"] or "",
        "heat": round(score, 4),
        "favourites": latest.favourites,
        "views": latest.views,
        "fav_per_hour": round(vel.fav_per_min * 60, 1) if vel.fav_per_min is not None else None,
        "view_per_hour": round(vel.view_per_min * 60, 1) if vel.view_per_min is not None else None,
        "age_minutes": round(age_minutes, 1),
        "measured": vel.measured,
        "baseline": round(baseline, 2) if baseline else None,
        "discount_pct": discount,
        "alerted": alerted,
        "sold": sold,
        "seconds_to_sell": seconds_to_sell,
    }


def build_feed(state, config: Config, bar, baselines, *, now: float,
               limit: int = 120) -> dict:
    """Render the current state of play as the payload the website consumes."""
    cfg = config.alerts
    grouped = state.samples_by_item(now - cfg.velocity_window_min * 120)
    metas = state.meta_for(list(grouped.keys()))
    alerted = state.alerted_ids()

    entries: list[dict] = []
    sell_times: list[int] = []

    for item_id, samples in grouped.items():
        meta = metas.get(item_id)
        if meta is None:
            continue

        # Same wearability gates as the alert path — the site must never show
        # something your phone would have refused to tell you about.
        if not passes_condition(meta["condition"] or "", config.quality_floor):
            continue
        allowed = config.allowed_sizes(meta["gender"] or "", meta["garment_type"] or "")
        if not size_matches(meta["garment_type"] or "", meta["size"] or "", allowed):
            continue

        age_minutes = state.age_seconds(meta, now) / 60.0
        if age_minutes > cfg.max_age_minutes:
            continue

        vel = velocity(samples, now=now, listed_ts=meta["listed_ts"],
                       window_min=cfg.velocity_window_min)
        if vel is None:
            continue

        gone_at = meta["gone_at"]
        seconds_to_sell = None
        if gone_at and meta["listed_ts"]:
            seconds_to_sell = int(gone_at - meta["listed_ts"])
            sell_times.append(seconds_to_sell)

        score = heat(vel, fav_population=bar.fav_population,
                     view_population=bar.view_population,
                     fav_weight=cfg.fav_weight, view_weight=cfg.view_weight)

        entries.append(_entry(
            meta, vel, score, samples[-1], age_minutes=age_minutes,
            baseline=baselines.median_for(meta["brand"] or "", meta["category"] or ""),
            alerted=item_id in alerted,
            sold=bool(gone_at),
            seconds_to_sell=seconds_to_sell,
        ))

    entries.sort(key=lambda e: e["heat"], reverse=True)
    entries = entries[:limit]

    return {
        "version": FEED_VERSION,
        "generated_at": datetime.fromtimestamp(now, tz=timezone.utc)
                                .replace(microsecond=0).isoformat(),
        # The live bar, surfaced so the page can show what it currently takes to
        # trigger an alert — this is what makes the site a tuning instrument.
        "bar": {
            "favourites_per_hour": round(bar.fav_cut * 60, 2),
            "adaptive": bar.adaptive,
            "sample_size": bar.sample_size,
        },
        "counts": {
            "tracked": len(entries),
            "alerted": sum(1 for e in entries if e["alerted"]),
            "sold": sum(1 for e in entries if e["sold"]),
        },
        "median_seconds_to_sell": (
            int(statistics.median(sell_times)) if sell_times else None
        ),
        "items": entries,
    }


def _fingerprint(feed: dict) -> list[tuple]:
    """The part of a feed that is worth republishing for.

    Heat is rounded hard: the rates wobble slightly every cycle as the anchor
    sample moves, and without this the feed would look "changed" on every single
    poll and push a commit every minute.
    """
    return [
        (e["id"], e["alerted"], e["sold"], round(e["heat"], 2))
        for e in feed.get("items", [])
    ]


def feed_changed(previous: dict | None, current: dict) -> bool:
    """True if the feed has materially changed since the last publish."""
    if previous is None:
        return True
    return _fingerprint(previous) != _fingerprint(current)

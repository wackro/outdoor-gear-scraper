"""Render the static shell into docs/ (published by GitHub Pages).

The page no longer contains any listings. It is a shell that fetches a JSON feed
the poller publishes every few minutes, because hot listings sell within the hour
and anything baked in at build time would be hours stale by the time it was read.

This module also writes a *fallback* copy of that feed, built from the alerts
already recorded in the committed database. It's what the page falls back to
before the poller has ever run, or if the feed branch is unreachable — so the
site degrades to "here's what we caught recently" rather than to an error.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..storage.db import Database

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs"

log = logging.getLogger(__name__)

CURRENCY_SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$"}
TYPE_LABELS = {"clothes": "Clothes", "trousers": "Trousers", "shoes": "Shoes", "bags": "Bags"}
SECTIONS = ("men", "women", "bags")
SECTION_LABELS = {"men": "Men's", "women": "Women's", "bags": "Bags"}

FALLBACK_FEED_NAME = "hot.json"


def resolve_feed_url(configured: str, branch: str) -> str:
    """Where the page should fetch live data from.

    Prefers an explicit config value; otherwise derives the raw-content URL from
    $GITHUB_REPOSITORY, which Actions always sets. Falling back to the local
    `hot.json` means the page still works when opened straight off disk.
    """
    if configured:
        return configured
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        return f"https://raw.githubusercontent.com/{repo}/{branch}/{FALLBACK_FEED_NAME}"
    return FALLBACK_FEED_NAME


def _section(gender: str, garment_type: str) -> str:
    return "bags" if garment_type == "bags" else (gender or "men")


def build_fallback_feed(db: Database, *, limit: int = 120) -> dict:
    """A feed built from alerts already in the committed DB.

    Deliberately the same shape as the poller's live feed, so the page needs no
    special case: it renders whichever it gets.
    """
    try:
        rows = db.conn.execute(
            """
            SELECT a.item_id, a.alerted_at, a.heat, a.fav_rate, a.view_rate,
                   a.price, a.baseline, a.sold_at, a.seconds_to_sell,
                   i.title, i.brand, i.brand_title, i.gender, i.garment_type,
                   i.size, i.condition, i.url, i.image_url, i.currency,
                   i.favourite_count, i.view_count, i.listed_ts, i.active
            FROM alerted a
            JOIN items i ON i.id = a.item_id
            ORDER BY a.alerted_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error as exc:
        # A database predating the alert tables (or opened read-only, so the
        # migration never ran) should still produce a page. An empty fallback is
        # correct here anyway: the live feed is what the page actually shows.
        log.warning("No alert history available for the fallback feed: %s", exc)
        rows = []

    now = datetime.now(timezone.utc)
    items = []
    sell_times = []
    for row in rows:
        price = float(row["price"] or 0)
        baseline = row["baseline"]
        discount = None
        if baseline and baseline > 0 and price:
            discount = round(max(0.0, 1 - price / baseline), 4)
        age_minutes = None
        if row["listed_ts"]:
            age_minutes = round((now.timestamp() - float(row["listed_ts"])) / 60.0, 1)
        if row["seconds_to_sell"]:
            sell_times.append(int(row["seconds_to_sell"]))
        items.append({
            "id": row["item_id"],
            "title": row["title"] or "Untitled listing",
            "brand": row["brand"] or "",
            "brand_title": row["brand_title"] or "",
            "gender": row["gender"] or "",
            "type": row["garment_type"] or "",
            "section": _section(row["gender"] or "", row["garment_type"] or ""),
            "price": price,
            "currency": row["currency"] or "GBP",
            "size": row["size"] or "",
            "condition": row["condition"] or "",
            "url": row["url"] or "",
            "image_url": row["image_url"] or "",
            "heat": row["heat"] or 0.0,
            "favourites": row["favourite_count"],
            "views": row["view_count"],
            "fav_per_hour": row["fav_rate"],
            "view_per_hour": row["view_rate"],
            "age_minutes": age_minutes,
            "measured": True,
            "baseline": baseline,
            "discount_pct": discount,
            "alerted": True,
            "sold": bool(row["sold_at"]) or not row["active"],
            "seconds_to_sell": row["seconds_to_sell"],
        })

    sell_times.sort()
    return {
        "version": 1,
        "generated_at": now.replace(microsecond=0).isoformat(),
        "stale_fallback": True,   # so it's never mistaken for live data
        "bar": {"favourites_per_hour": None, "adaptive": False, "sample_size": 0},
        "counts": {
            "tracked": len(items),
            "alerted": len(items),
            "sold": sum(1 for i in items if i["sold"]),
        },
        "median_seconds_to_sell": (
            sell_times[len(sell_times) // 2] if sell_times else None
        ),
        "items": items,
    }


def _inline_json(payload: dict) -> str:
    """JSON safe to embed inside a <script> tag.

    Escaping `<` prevents a listing title containing "</script>" from breaking
    out of the block — Jinja's autoescaping does not apply inside script tags.
    """
    return (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        .replace("<", "\\u003c")
    )


def render_site(
    db: Database,
    *,
    currency: str = "GBP",
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    feed_url: str = "",
    feed_branch: str = "hot-feed",
    refresh_sec: int = 60,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    fallback = build_fallback_feed(db)
    html = env.get_template("index.html").render(
        bootstrap_feed=_inline_json(fallback),
        sections=[(s, SECTION_LABELS[s]) for s in SECTIONS],
        type_labels=TYPE_LABELS,
        currency_symbol=CURRENCY_SYMBOLS.get(currency, currency + " "),
        feed_url=resolve_feed_url(feed_url, feed_branch),
        refresh_sec=refresh_sec,
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    (output_dir / "index.html").write_text(html, encoding="utf-8")

    # Also written as a file, so a page served without its inline bootstrap
    # (or a client fetching directly) still has somewhere to read.
    (output_dir / FALLBACK_FEED_NAME).write_text(
        json.dumps(fallback, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )

    static_out = output_dir / "static"
    if static_out.exists():
        shutil.rmtree(static_out)
    shutil.copytree(STATIC_DIR, static_out)
    (output_dir / ".nojekyll").touch()

    return output_dir / "index.html"

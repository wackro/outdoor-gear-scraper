"""Render the static deals site into docs/ (published by GitHub Pages)."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..storage.db import Database

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "docs"

CURRENCY_SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$"}
TYPE_LABELS = {"clothes": "Clothes", "trousers": "Trousers", "shoes": "Shoes", "bags": "Bags"}
SECTIONS = ("men", "women", "bags")
SECTION_LABELS = {"men": "Men's", "women": "Women's", "bags": "Bags"}


def _humanize(name: str) -> str:
    return name.replace("_", " ").title()


def render_site(
    db: Database,
    *,
    currency: str = "GBP",
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = db.deals_for_site()
    deals = []
    for row in rows:
        gender = row["gender"] or "men"
        gtype = row["garment_type"] or "clothes"
        section = "bags" if gtype == "bags" else gender
        deals.append(
            {
                "title": row["title"] or "Untitled listing",
                "gender": gender,
                "section": section,
                "type": gtype,
                "type_label": TYPE_LABELS.get(gtype, "Clothes"),
                "brand": row["brand"],
                "brand_label": _humanize(row["brand"]),
                "brand_title": row["brand_title"],
                "condition": row["condition"] or "",
                "price": row["price"],
                "baseline": row["baseline"],
                "baseline_src": row["baseline_src"],
                "discount_pct": round(row["discount_pct"] * 100),
                "deal_score": row["deal_score"],
                "size": row["size"],
                "url": row["url"],
                "image_url": row["image_url"],
                "first_seen": row["first_seen"],
            }
        )

    brands = sorted({d["brand"] for d in deals})
    types = [t for t in ("clothes", "trousers", "shoes", "bags") if any(d["type"] == t for d in deals)]
    counts = {s: sum(1 for d in deals if d["section"] == s) for s in SECTIONS}

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    html = env.get_template("index.html").render(
        deals=deals,
        brand_filters=[(b, _humanize(b)) for b in brands],
        type_filters=[(t, TYPE_LABELS[t]) for t in types],
        sections=[(s, SECTION_LABELS[s]) for s in SECTIONS],
        counts=counts,
        currency_symbol=CURRENCY_SYMBOLS.get(currency, currency + " "),
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )

    (output_dir / "index.html").write_text(html, encoding="utf-8")

    static_out = output_dir / "static"
    if static_out.exists():
        shutil.rmtree(static_out)
    shutil.copytree(STATIC_DIR, static_out)
    (output_dir / ".nojekyll").touch()

    return output_dir / "index.html"

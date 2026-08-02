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
        deals.append(
            {
                "title": row["title"] or "Untitled listing",
                "brand": row["brand"],
                "brand_label": _humanize(row["brand"]),
                "brand_title": row["brand_title"],
                "category": row["category"],
                "category_label": _humanize(row["category"]),
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
    categories = sorted({d["category"] for d in deals})

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("index.html")
    html = template.render(
        deals=deals,
        brand_filters=[(b, _humanize(b)) for b in brands],
        category_filters=[(c, _humanize(c)) for c in categories],
        currency_symbol=CURRENCY_SYMBOLS.get(currency, currency + " "),
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )

    (output_dir / "index.html").write_text(html, encoding="utf-8")

    # Copy static assets (css/js) alongside the page.
    static_out = output_dir / "static"
    if static_out.exists():
        shutil.rmtree(static_out)
    shutil.copytree(STATIC_DIR, static_out)

    # Disable Jekyll processing so GitHub Pages serves files as-is.
    (output_dir / ".nojekyll").touch()

    return output_dir / "index.html"

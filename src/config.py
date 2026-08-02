"""Load and validate the YAML configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.yaml"


@dataclass(frozen=True)
class Brand:
    name: str
    id: int | None = None          # optional: resolved by name when omitted
    search: str | None = None      # display name to search Vinted for (defaults to the key)
    threshold: float | None = None
    rrp: dict[str, float] = field(default_factory=dict)

    @property
    def search_text(self) -> str:
        return self.search or self.name.replace("_", " ")


@dataclass(frozen=True)
class ScrapeConfig:
    per_page: int = 96
    order: str = "newest_first"
    max_pages_per_query: int = 3
    min_delay_sec: float = 2.5
    max_delay_sec: float = 5.0
    impersonate: str = "chrome"
    max_retries: int = 3


@dataclass(frozen=True)
class DealsConfig:
    threshold: float = 0.30
    min_samples: int = 8
    window_days: int = 90
    stale_days: int = 5


GENDERS = ("men", "women")
GARMENT_TYPES = ("clothes", "trousers", "shoes")


@dataclass(frozen=True)
class Category:
    name: str          # unique key, e.g. "men_jackets"
    gender: str        # "men" | "women"
    type: str          # "clothes" | "trousers" | "shoes"
    search: str        # category title to resolve in the tree (e.g. "Jackets")
    id: int | None = None  # optional fixed catalog id (fallback / pin)


@dataclass(frozen=True)
class Config:
    currency: str
    base_url: str
    scrape: ScrapeConfig
    deals: DealsConfig
    categories: list[Category]
    brands: list[Brand]
    sizes: dict[str, dict[str, list[str]]]  # gender -> type -> allowed size tokens
    quality_floor: str

    def threshold_for(self, brand_name: str) -> float:
        for brand in self.brands:
            if brand.name == brand_name and brand.threshold is not None:
                return brand.threshold
        return self.deals.threshold

    def allowed_sizes(self, gender: str, garment_type: str) -> list[str]:
        return (self.sizes.get(gender) or {}).get(garment_type) or []


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load config from the given path, the CONFIG_PATH env var, or the default."""
    resolved = Path(path or os.environ.get("CONFIG_PATH") or DEFAULT_CONFIG_PATH)
    if not resolved.exists():
        raise FileNotFoundError(
            f"Config not found at {resolved}. Copy config/config.example.yaml to "
            f"config/config.yaml (or set CONFIG_PATH)."
        )

    raw = yaml.safe_load(resolved.read_text()) or {}

    categories_raw = raw.get("categories") or []
    if not categories_raw:
        raise ValueError("Config must define at least one entry under `categories`.")

    categories: list[Category] = []
    for entry in categories_raw:
        gender = entry.get("gender")
        gtype = entry.get("type")
        search = entry.get("search")
        if gender not in GENDERS:
            raise ValueError(f"Category {entry!r} needs gender one of {GENDERS}.")
        if gtype not in GARMENT_TYPES:
            raise ValueError(f"Category {entry!r} needs type one of {GARMENT_TYPES}.")
        if not search:
            raise ValueError(f"Category {entry!r} needs a `search` title.")
        name = entry.get("name") or f"{gender}_{search.lower().replace(' ', '_')}"
        categories.append(
            Category(
                name=name,
                gender=gender,
                type=gtype,
                search=search,
                id=int(entry["id"]) if entry.get("id") is not None else None,
            )
        )

    brands_raw = raw.get("brands") or {}
    if not brands_raw:
        raise ValueError("Config must define at least one entry under `brands`.")

    brands: list[Brand] = []
    for name, settings in brands_raw.items():
        settings = settings or {}
        brands.append(
            Brand(
                name=name,
                id=int(settings["id"]) if settings.get("id") is not None else None,
                search=settings.get("search"),
                threshold=settings.get("threshold"),
                rrp=settings.get("rrp") or {},
            )
        )

    sizes_raw = raw.get("sizes") or {}
    sizes = {
        gender: {
            gtype: [str(s) for s in (tokens or [])]
            for gtype, tokens in (sizes_raw.get(gender) or {}).items()
        }
        for gender in GENDERS
    }

    return Config(
        currency=raw.get("currency", "GBP"),
        base_url=raw.get("base_url", "https://www.vinted.co.uk").rstrip("/"),
        scrape=ScrapeConfig(**(raw.get("scrape") or {})),
        deals=DealsConfig(**(raw.get("deals") or {})),
        categories=categories,
        brands=brands,
        sizes=sizes,
        quality_floor=raw.get("quality", {}).get("floor", "Good"),
    )

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


@dataclass(frozen=True)
class Config:
    currency: str
    base_url: str
    scrape: ScrapeConfig
    deals: DealsConfig
    categories: dict[str, int]
    brands: list[Brand]

    def threshold_for(self, brand_name: str) -> float:
        for brand in self.brands:
            if brand.name == brand_name and brand.threshold is not None:
                return brand.threshold
        return self.deals.threshold


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load config from the given path, the CONFIG_PATH env var, or the default."""
    resolved = Path(path or os.environ.get("CONFIG_PATH") or DEFAULT_CONFIG_PATH)
    if not resolved.exists():
        raise FileNotFoundError(
            f"Config not found at {resolved}. Copy config/config.example.yaml to "
            f"config/config.yaml (or set CONFIG_PATH)."
        )

    raw = yaml.safe_load(resolved.read_text()) or {}

    categories = raw.get("categories") or {}
    if not categories:
        raise ValueError("Config must define at least one entry under `categories`.")

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

    return Config(
        currency=raw.get("currency", "GBP"),
        base_url=raw.get("base_url", "https://www.vinted.co.uk").rstrip("/"),
        scrape=ScrapeConfig(**(raw.get("scrape") or {})),
        deals=DealsConfig(**(raw.get("deals") or {})),
        categories={str(k): int(v) for k, v in categories.items()},
        brands=brands,
    )

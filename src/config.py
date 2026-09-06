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
class NightBackoff:
    """Poll less often when almost nothing is being listed.

    UK listing volume collapses overnight, so polling at the daytime rate then
    spends requests against a DataDome-protected endpoint to discover nothing.
    """
    start_hour: int = 1        # UTC, inclusive
    end_hour: int = 6          # UTC, exclusive
    multiplier: float = 4.0


@dataclass(frozen=True)
class PollConfig:
    """Pacing for the fast poller."""
    interval_sec: float = 60.0          # discovery sweep: page 1 of the firehose
    deep_interval_sec: float = 300.0    # deeper pages, for velocity on older items
    deep_pages: int = 3
    jitter: float = 0.2                 # +/- fraction, so the pattern isn't periodic
    session_refresh_min: float = 30.0   # re-bootstrap cookies this often
    threshold_refresh_sec: float = 600.0
    max_runtime_sec: float = 20700.0    # 5h45m: exit before the 6h Actions kill
    cooldown_base_sec: float = 60.0     # first backoff after a block
    cooldown_max_sec: float = 900.0
    circuit_breaker_failures: int = 8
    night: NightBackoff = field(default_factory=NightBackoff)

    # -- the website feed --
    feed_enabled: bool = True
    feed_branch: str = "hot-feed"       # force-pushed, single commit, no history
    feed_publish_interval_sec: float = 300.0
    feed_limit: int = 120


@dataclass(frozen=True)
class AlertsConfig:
    """What counts as alert-worthy, and where the alert goes."""
    enabled: bool = True
    channel: str = "ntfy"               # 'ntfy' | 'console'
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    burst_cap_per_hour: int = 10

    # -- what "hot" means --
    velocity_window_min: float = 30.0   # anchor rates against this much history
    floor_favourites_per_hour: float = 4.0
    min_favourites_gain: int = 3        # raw likes needed; guards short-span rates
    percentile: float = 99.0
    min_population: int = 200           # below this, the floor alone decides
    fav_weight: float = 0.7             # likes: strong intent, arrive later
    view_weight: float = 0.3            # views: weaker, but arrive earlier

    # -- which listings are eligible --
    min_age_minutes: float = 3.0        # younger than this, rates are meaningless
    max_age_minutes: float = 180.0      # older than this, the window has closed
    population_max_age_min: float = 60.0
    allow_bootstrap: bool = False       # act on single-sighting estimates?

    # -- price veto (secondary to hotness, never the trigger) --
    sanity_discount: float = 0.10       # hot AND at least this far under baseline
    max_price: float = 400.0


GENDERS = ("men", "women")
GARMENT_TYPES = ("clothes", "trousers", "shoes", "bags")


@dataclass(frozen=True)
class SiteConfig:
    """Where the published page looks for its live data."""
    # Left blank, the generator derives it from $GITHUB_REPOSITORY at render time.
    feed_url: str = ""
    refresh_sec: int = 60


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
    poll: PollConfig
    alerts: AlertsConfig
    site: SiteConfig
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


def _poll_config(raw: dict) -> PollConfig:
    """Build PollConfig, expanding the nested `night` block into its dataclass."""
    settings = dict(raw)
    night = settings.pop("night", None)
    return PollConfig(**settings, night=NightBackoff(**(night or {})))


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
        poll=_poll_config(raw.get("poll") or {}),
        alerts=AlertsConfig(**(raw.get("alerts") or {})),
        site=SiteConfig(**(raw.get("site") or {})),
        categories=categories,
        brands=brands,
        sizes=sizes,
        quality_floor=raw.get("quality", {}).get("floor", "Good"),
    )

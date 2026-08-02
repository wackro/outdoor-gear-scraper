"""Price baselines.

A baseline is the reference price a listing is judged against. The default
`HistoryBaseline` derives it from our own observed price history (robust median
per brand+category). `RRPBaseline` reads configured retail prices. `Composite`
prefers history where we have enough data and falls back to RRP otherwise —
this is the "hybrid" strategy, and the seam where richer RRP data can be added
later without touching the rest of the pipeline.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Protocol

from ..config import Config
from ..storage.db import Observation

BRAND_LEVEL = "*"  # sentinel category for brand-wide (all-category) baselines


@dataclass(frozen=True)
class BaselineStat:
    median: float
    mad: float
    sample_size: int


@dataclass(frozen=True)
class Reference:
    """A reference price plus how it was derived."""
    value: float
    source: str            # 'history' | 'rrp'
    spread: float | None   # robust spread (MAD) when known, else None


def _mad(values: list[float], median: float) -> float:
    """Median absolute deviation — a robust, outlier-resistant spread."""
    if not values:
        return 0.0
    return statistics.median([abs(v - median) for v in values])


def compute_stats(prices: list[float]) -> BaselineStat:
    median = statistics.median(prices)
    return BaselineStat(median=median, mad=_mad(prices, median), sample_size=len(prices))


def compute_baselines(
    observations: list[Observation],
) -> tuple[dict[tuple[str, str], BaselineStat], dict[str, BaselineStat]]:
    """Return (per brand+category stats, per-brand aggregate stats)."""
    by_bracket: dict[tuple[str, str], list[float]] = {}
    by_brand: dict[str, list[float]] = {}
    for obs in observations:
        by_bracket.setdefault((obs.brand, obs.category), []).append(obs.price)
        by_brand.setdefault(obs.brand, []).append(obs.price)

    bracket_stats = {key: compute_stats(prices) for key, prices in by_bracket.items()}
    brand_stats = {brand: compute_stats(prices) for brand, prices in by_brand.items()}
    return bracket_stats, brand_stats


class BaselineProvider(Protocol):
    def reference(self, brand: str, category: str) -> Reference | None: ...


class HistoryBaseline:
    """Reference = median of observed prices, if we have enough of them.

    Falls back from the specific brand+category bracket to a brand-wide median
    before giving up, so a brand with sparse per-category data can still score.
    """

    def __init__(
        self,
        bracket_stats: dict[tuple[str, str], BaselineStat],
        brand_stats: dict[str, BaselineStat],
        min_samples: int,
    ):
        self.bracket_stats = bracket_stats
        self.brand_stats = brand_stats
        self.min_samples = min_samples

    def reference(self, brand: str, category: str) -> Reference | None:
        stat = self.bracket_stats.get((brand, category))
        if stat and stat.sample_size >= self.min_samples:
            return Reference(stat.median, "history", stat.mad)
        brand_stat = self.brand_stats.get(brand)
        if brand_stat and brand_stat.sample_size >= self.min_samples:
            return Reference(brand_stat.median, "history", brand_stat.mad)
        return None


class RRPBaseline:
    """Reference = configured retail price for a brand+category, when present."""

    def __init__(self, config: Config):
        self._rrp: dict[tuple[str, str], float] = {}
        for brand in config.brands:
            for category, price in (brand.rrp or {}).items():
                self._rrp[(brand.name, category)] = float(price)

    def reference(self, brand: str, category: str) -> Reference | None:
        price = self._rrp.get((brand, category))
        if price is None:
            return None
        return Reference(price, "rrp", None)


class CompositeBaseline:
    """Try each provider in order; first hit wins. History first, then RRP."""

    def __init__(self, *providers: BaselineProvider):
        self.providers = providers

    def reference(self, brand: str, category: str) -> Reference | None:
        for provider in self.providers:
            ref = provider.reference(brand, category)
            if ref is not None:
                return ref
        return None

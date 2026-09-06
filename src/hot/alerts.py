"""Deciding which hot listings are worth interrupting someone for.

Hotness is the trigger; price is only a veto. That inversion is the point of the
rework — the old pipeline asked "is this cheap relative to a median?", which on a
long tail of thin brackets produces things like a £1 backpack strap against a
£33 backpack median. Asking "are people piling onto this right now?" instead
lets the market do the valuation, and it works for rare brands that will never
have enough price history to have a baseline at all.
"""
from __future__ import annotations

import logging
import sqlite3

from ..config import Config
from ..filters import passes_condition, size_matches
from ..notify import Alert
from .hotness import Velocity, heat, velocity
from .thresholds import Bar, clears

log = logging.getLogger(__name__)


class BaselineLookup:
    """Read-only view of the baselines the daily job materialised.

    The hot path never recomputes these — it would mean scanning 136k price
    observations every 60 seconds to produce a number that changes daily.
    """

    def __init__(self, rows: dict[tuple[str, str], tuple[float, int]]):
        self._rows = rows

    @classmethod
    def from_db(cls, conn: sqlite3.Connection, min_samples: int) -> "BaselineLookup":
        rows: dict[tuple[str, str], tuple[float, int]] = {}
        try:
            cursor = conn.execute(
                "SELECT brand, category, median, sample_size FROM baselines"
            )
        except sqlite3.Error as exc:
            # A missing table just means the daily job has not run yet. The price
            # veto is optional, so degrade to "no baselines" rather than dying.
            log.warning("No baselines available (%s); price sanity check disabled.", exc)
            return cls({})
        for row in cursor:
            if row[2] and row[3] >= min_samples:
                rows[(row[0], row[1])] = (float(row[2]), int(row[3]))
        return cls(rows)

    def median_for(self, brand: str, category: str) -> float | None:
        entry = self._rows.get((brand, category))
        return entry[0] if entry else None

    def __len__(self) -> int:
        return len(self._rows)


def _passes_gates(meta: sqlite3.Row, config: Config) -> bool:
    """The user's own hard filters: right size, good enough condition.

    Reused verbatim from the daily pipeline (`src.filters`) so the site and the
    alerts can never disagree about what counts as wearable.
    """
    if not passes_condition(meta["condition"] or "", config.quality_floor):
        return False
    allowed = config.allowed_sizes(meta["gender"] or "", meta["garment_type"] or "")
    return size_matches(meta["garment_type"] or "", meta["size"] or "", allowed)


def _to_alert(
    meta: sqlite3.Row,
    vel: Velocity,
    score: float,
    latest,
    *,
    age_minutes: float,
    baseline: float | None,
    urgent: bool,
) -> Alert:
    discount = None
    if baseline and baseline > 0:
        discount = max(0.0, 1 - meta["price"] / baseline)
    return Alert(
        item_id=meta["item_id"],
        title=meta["title"] or "",
        brand_title=meta["brand"] or "",
        price=float(meta["price"]),
        currency=meta["currency"] or "GBP",
        url=meta["url"] or "",
        image_url=meta["image_url"] or "",
        size=meta["size"] or "",
        condition=meta["condition"] or "",
        heat=score,
        favourites=latest.favourites,
        views=latest.views,
        fav_per_hour=vel.fav_per_min * 60 if vel.fav_per_min is not None else None,
        view_per_hour=vel.view_per_min * 60 if vel.view_per_min is not None else None,
        age_minutes=age_minutes,
        measured=vel.measured,
        baseline=baseline,
        discount_pct=discount,
        urgent=urgent,
    )


def evaluate(
    state,
    config: Config,
    bar: Bar,
    baselines: BaselineLookup,
    *,
    now: float,
) -> list[Alert]:
    """Score every tracked listing and return the ones worth pushing.

    Ordered hottest first, so that if the burst cap bites it drops the weakest
    alerts rather than whichever happened to be evaluated last.
    """
    cfg = config.alerts
    window_sec = cfg.velocity_window_min * 60
    grouped = state.samples_by_item(now - window_sec * 2)
    if not grouped:
        return []

    metas = state.meta_for(list(grouped.keys()))
    candidates: list[tuple[float, Alert]] = []

    for item_id, samples in grouped.items():
        meta = metas.get(item_id)
        if meta is None:
            continue

        # Cheapest checks first — most listings fail the size gate, and there is
        # no point computing velocity for something you could never wear.
        if not _passes_gates(meta, config):
            continue
        if state.has_alerted(item_id):
            continue

        age_minutes = state.age_seconds(meta, now) / 60.0
        if age_minutes < cfg.min_age_minutes:
            # Too young to judge: dividing a like count by a near-zero age
            # manufactures enormous rates out of nothing.
            continue
        if age_minutes > cfg.max_age_minutes:
            # Past its window. If it hasn't sold by now it isn't the kind of
            # bargain this system exists to catch.
            continue

        vel = velocity(
            samples, now=now, listed_ts=meta["listed_ts"],
            window_min=cfg.velocity_window_min,
        )
        if vel is None or not clears(vel, bar, min_gain=cfg.min_favourites_gain):
            continue
        if not vel.measured and not cfg.allow_bootstrap:
            # An age-derived rate from a single sighting is a guess. Off by
            # default: it is the main source of false alarms.
            continue

        price = float(meta["price"])
        if price > cfg.max_price:
            continue

        baseline = baselines.median_for(meta["brand"] or "", meta["category"] or "")
        if baseline and price > baseline * (1 - cfg.sanity_discount):
            # Hot but not cheap. Popular at a fair price is not a bargain — this
            # is the veto that stops us alerting on every in-demand listing.
            continue

        score = heat(
            vel,
            fav_population=bar.fav_population,
            view_population=bar.view_population,
            fav_weight=cfg.fav_weight,
            view_weight=cfg.view_weight,
        )

        # Escalate only when the crowd and the price agree. That combination is
        # rare enough to justify overriding a silent phone at 3am.
        urgent = bool(
            baseline and price <= baseline * (1 - config.threshold_for(meta["brand"] or ""))
        )

        candidates.append((
            score,
            _to_alert(meta, vel, score, samples[-1], age_minutes=age_minutes,
                      baseline=baseline, urgent=urgent),
        ))

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [alert for _, alert in candidates]


def population_rates(
    state, config: Config, *, now: float
) -> tuple[list[float], list[float]]:
    """Velocity samples for building the adaptive bar.

    Restricted to listings inside their first hour. Scoring a fresh listing
    against week-old ones would make everything look hot: an old listing's rate
    has long since decayed to zero, dragging the distribution — and therefore the
    bar — down for everyone.
    """
    cfg = config.alerts
    grouped = state.samples_by_item(now - cfg.velocity_window_min * 120)
    metas = state.meta_for(list(grouped.keys()))

    fav_rates: list[float] = []
    view_rates: list[float] = []
    for item_id, samples in grouped.items():
        meta = metas.get(item_id)
        if meta is None:
            continue
        if state.age_seconds(meta, now) > cfg.population_max_age_min * 60:
            continue
        vel = velocity(
            samples, now=now, listed_ts=meta["listed_ts"],
            window_min=cfg.velocity_window_min,
        )
        # Measured rates only. Bootstrap estimates are noisy, and letting them
        # into the population would distort the very percentile they're judged by.
        if vel is None or not vel.measured:
            continue
        if vel.fav_per_min is not None:
            fav_rates.append(vel.fav_per_min)
        if vel.view_per_min is not None:
            view_rates.append(vel.view_per_min)
    return fav_rates, view_rates

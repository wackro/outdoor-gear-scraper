"""How fast a listing is gaining attention.

The premise of the whole hot path: a genuine bargain is recognised by the market
almost immediately, so the rate at which a listing accrues views and favourites
is a better bargain detector than its price relative to a median. A £1 listing
with a broken zip looks like a 97% discount and nobody wants it; a fairly-priced
Arc'teryx shell nobody wants stays cold. Attention separates them.

Two signals, deliberately combined:

  - **views** accrue first, so they detect earlier — this matters because
    attention is inherently a lagging signal and every minute counts.
  - **favourites** are a much stronger statement of intent, so they carry more
    weight but arrive later.

Everything here is a pure function over samples. No clock, no network, no
database — so the maths can be tested exhaustively without any of them.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

# Rates measured over a very short span are dominated by noise: one like two
# seconds after we happened to poll is not "1800 likes/hour". We divide by at
# least this many minutes, which damps short spans instead of rejecting them.
MIN_SPAN_MIN = 1.0

# Never anchor a rate to a sample older than this. A listing that got 40 likes
# in its first ten minutes and has been flat for three hours is not still hot,
# and a long anchor would keep insisting that it is.
DEFAULT_WINDOW_MIN = 30.0


@dataclass(frozen=True)
class Sample:
    """One observation of a listing's attention counters.

    Both counters default to None — "we don't know" — because Vinted omits them
    for some listings and defaulting to 0 would fabricate a reading.
    """
    observed: float           # unix seconds
    favourites: int | None = None
    views: int | None = None


@dataclass(frozen=True)
class Velocity:
    """Attention gained per minute, and how confident we are in that number."""
    fav_per_min: float | None
    view_per_min: float | None
    source: str               # 'delta' (measured) | 'bootstrap' (inferred from age)
    span_min: float           # the interval the rate was measured over
    fav_gain: int | None = None    # raw likes gained over that span
    view_gain: int | None = None

    @property
    def measured(self) -> bool:
        """True when this came from two real observations, not an age estimate."""
        return self.source == "delta"


def _gain(current: int | None, earlier: int | None) -> int | None:
    """Raw growth between two counter readings.

    Returns None when either reading is unknown — Vinted omits these counters for
    some listings and inventing a zero would fabricate a signal.

    A counter that goes *backwards* (someone un-favourites, or Vinted corrects
    itself) is floored at zero rather than producing a negative gain: the absence
    of growth is the useful fact, and a negative "hotness" is meaningless.
    """
    if current is None or earlier is None:
        return None
    return max(current - earlier, 0)


def _rate(gain: int | None, span_min: float) -> float | None:
    """Convert a raw gain into a per-minute rate."""
    if gain is None:
        return None
    return gain / max(span_min, MIN_SPAN_MIN)


def velocity(
    samples: list[Sample],
    *,
    now: float,
    listed_ts: int | None = None,
    window_min: float = DEFAULT_WINDOW_MIN,
) -> Velocity | None:
    """Attention velocity for one listing, from its observation history.

    `samples` must be in ascending time order. We anchor against the *oldest*
    sample still inside `window_min` rather than the previous poll, because at a
    60-second cadence consecutive readings are usually identical — most listings
    gain a like every few minutes at best, so a poll-to-poll delta is almost
    always zero and tells us nothing. A ~30-minute span gives the counter room to
    actually move while staying recent enough to mean "hot *now*".

    With only one sample there is no delta to take, so we fall back to inferring
    a rate from the listing's age (`source='bootstrap'`). That estimate is much
    weaker — it averages over the listing's whole life rather than measuring the
    current moment — so callers should hold bootstrap-only items to a higher bar.
    """
    if not samples:
        return None

    latest = samples[-1]
    cutoff = now - window_min * 60.0

    anchor = None
    for sample in samples[:-1]:
        if sample.observed >= cutoff:
            anchor = sample
            break

    if anchor is not None:
        span_min = (latest.observed - anchor.observed) / 60.0
        if span_min > 0:
            fav_gain = _gain(latest.favourites, anchor.favourites)
            view_gain = _gain(latest.views, anchor.views)
            return Velocity(
                fav_per_min=_rate(fav_gain, span_min),
                view_per_min=_rate(view_gain, span_min),
                source="delta",
                span_min=span_min,
                fav_gain=fav_gain,
                view_gain=view_gain,
            )

    # Only one usable sample — infer from how long the listing has existed.
    if listed_ts is None:
        return None
    age_min = (now - listed_ts) / 60.0
    if age_min <= 0:
        return None
    return Velocity(
        fav_per_min=_rate(latest.favourites, age_min),
        view_per_min=_rate(latest.views, age_min),
        source="bootstrap",
        span_min=age_min,
        fav_gain=latest.favourites,
        view_gain=latest.views,
    )


def pct_rank(value: float, sorted_population: list[float]) -> float:
    """Where `value` sits in `sorted_population`, as a fraction from 0 to 1.

    Percentile rank rather than a z-score because these distributions are wildly
    long-tailed — the vast majority of listings never get a single like, so the
    mean and standard deviation are both dominated by zeros and a z-score would
    call almost anything an outlier. Rank is immune to the shape of the tail.
    """
    if not sorted_population:
        return 0.0
    return bisect_right(sorted_population, value) / len(sorted_population)


def heat(
    vel: Velocity,
    *,
    fav_population: list[float],
    view_population: list[float],
    fav_weight: float = 0.7,
    view_weight: float = 0.3,
) -> float:
    """Combine the two velocities into a single 0..1 score.

    Each rate is converted to a percentile rank against listings observed
    recently, so the score answers "hot compared to what else is on the site
    right now" rather than being pinned to a constant that would need retuning
    every season.

    Whichever signals are available are renormalised to sum to 1, so an item with
    no view count is scored on favourites alone rather than being penalised to
    70% of its true heat for a gap in Vinted's data.
    """
    parts: list[tuple[float, float]] = []
    if vel.fav_per_min is not None:
        parts.append((fav_weight, pct_rank(vel.fav_per_min, fav_population)))
    if vel.view_per_min is not None:
        parts.append((view_weight, pct_rank(vel.view_per_min, view_population)))

    total_weight = sum(weight for weight, _ in parts)
    if total_weight <= 0:
        return 0.0
    return sum(weight * rank for weight, rank in parts) / total_weight

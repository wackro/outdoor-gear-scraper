"""The adaptive bar a listing must clear to be worth waking someone up for.

A fixed rule like "alert on 4 likes an hour" is wrong within a week: Vinted's
traffic swings with the season, the day and the hour, so a constant either
floods you in December or goes silent in August. Instead the bar is a high
percentile of what listings are *currently* achieving, which keeps alert volume
roughly stable without anyone retuning it.

An absolute floor sits underneath. Percentiles are relative, and the 99th
percentile of a dead Tuesday morning is still dead — without a floor the poller
would faithfully alert on the least-cold listing available. The floor is what
stops "hottest of a cold bunch" from reaching your phone.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Below this many observations a percentile is not a distribution, it's an
# anecdote. Until we have this many we run on the absolute floor alone.
MIN_POPULATION = 200


@dataclass(frozen=True)
class Bar:
    """The current alerting bar, plus the populations that produced it."""
    fav_population: list[float] = field(default_factory=list)   # sorted, per minute
    view_population: list[float] = field(default_factory=list)  # sorted, per minute
    fav_cut: float = 0.0        # favourites/min a listing must beat
    adaptive: bool = False      # True once the percentile is driving the bar

    @property
    def sample_size(self) -> int:
        return len(self.fav_population)


def percentile(sorted_values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile of an already-sorted list. `pct` is 0..100."""
    if not sorted_values:
        return None
    rank = max(1, min(len(sorted_values), round(pct / 100.0 * len(sorted_values))))
    return sorted_values[rank - 1]


def build_bar(
    fav_rates: list[float],
    view_rates: list[float],
    *,
    pct: float = 99.0,
    floor_per_hour: float = 4.0,
    min_population: int = MIN_POPULATION,
) -> Bar:
    """Compute the current bar from recently observed rates.

    `fav_rates` and `view_rates` must come from listings in their *first hour*.
    Scoring a fresh listing against week-old ones would make everything look hot
    by comparison — an old listing's rate has decayed to nearly zero, so it drags
    the distribution down and lowers the bar for everyone.

    The bar is the higher of the floor and the percentile, so a quiet market
    cannot lower it and a busy one cannot flood you.
    """
    fav_sorted = sorted(fav_rates)
    view_sorted = sorted(view_rates)
    floor_per_min = floor_per_hour / 60.0

    if len(fav_sorted) < min_population:
        # Cold start: no usable distribution yet, so the floor is the whole rule.
        return Bar(fav_sorted, view_sorted, fav_cut=floor_per_min, adaptive=False)

    cut = percentile(fav_sorted, pct)
    if cut is None:
        return Bar(fav_sorted, view_sorted, fav_cut=floor_per_min, adaptive=False)

    return Bar(
        fav_population=fav_sorted,
        view_population=view_sorted,
        fav_cut=max(floor_per_min, cut),
        adaptive=cut >= floor_per_min,
    )


def clears(vel, bar: Bar, *, min_gain: int = 3) -> bool:
    """True if this velocity is hot enough to be worth an alert.

    Favourites are the gate, not views. Views are noisy — a listing surfaced high
    in a popular search accrues them without anyone actually wanting it — so
    views inform the *ranking* (via `heat`) while favourites decide the *trigger*.
    A like is somebody saying they want this specific thing.

    `min_gain` guards against the rate maths' worst failure mode. A rate is a
    gain divided by a span, so over a short span a *single* like implies an
    enormous hourly figure — one like a minute after we first saw a listing reads
    as 60 likes/hour and sails over any sane floor. Requiring a few actual likes
    makes the trigger robust to that, because no amount of arithmetic can turn
    one like into a crowd.
    """
    if vel is None or vel.fav_per_min is None:
        return False
    if vel.fav_gain is not None and vel.fav_gain < min_gain:
        return False
    return vel.fav_per_min >= bar.fav_cut

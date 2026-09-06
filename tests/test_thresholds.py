"""The alerting bar: adaptive when there's data, floored when there isn't."""
from src.hot.hotness import Sample, velocity
from src.hot.thresholds import build_bar, clears, percentile

NOW = 1_000_000.0


class TestPercentile:
    def test_empty(self):
        assert percentile([], 99) is None

    def test_nearest_rank(self):
        assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
        assert percentile([1.0, 2.0, 3.0, 4.0], 100) == 4.0

    def test_never_indexes_out_of_range(self):
        assert percentile([1.0], 0) == 1.0
        assert percentile([1.0], 100) == 1.0


class TestBuildBar:
    def test_cold_start_uses_the_floor_alone(self):
        # With no history a percentile is meaningless — the floor is the rule.
        bar = build_bar([], [], floor_per_hour=6.0)
        assert bar.fav_cut == 0.1          # 6/hour in per-minute terms
        assert not bar.adaptive

    def test_thin_population_still_uses_the_floor(self):
        bar = build_bar([5.0] * 10, [], floor_per_hour=4.0, min_population=200)
        assert not bar.adaptive
        assert bar.fav_cut == 4.0 / 60

    def test_busy_market_raises_the_bar_above_the_floor(self):
        # 1000 samples topping out at 2/min: p99 should drive the cut.
        rates = [0.0] * 900 + [2.0] * 100
        bar = build_bar(rates, [], pct=99, floor_per_hour=4.0, min_population=200)
        assert bar.adaptive
        assert bar.fav_cut == 2.0

    def test_quiet_market_cannot_lower_the_bar_below_the_floor(self):
        # Without the floor this would alert on the least-cold listing available.
        bar = build_bar([0.0] * 1000, [], pct=99, floor_per_hour=4.0,
                        min_population=200)
        assert bar.fav_cut == 4.0 / 60
        assert not bar.adaptive


class TestClears:
    def _vel(self, favourites, minutes=60):
        return velocity(
            [Sample(NOW - minutes * 60, 0), Sample(NOW, favourites)],
            now=NOW, window_min=minutes + 1,
        )

    def test_beats_the_bar(self):
        bar = build_bar([], [], floor_per_hour=4.0)
        assert clears(self._vel(10), bar)

    def test_below_the_bar(self):
        bar = build_bar([], [], floor_per_hour=4.0)
        assert not clears(self._vel(1), bar)

    def test_exactly_at_the_bar_counts(self):
        bar = build_bar([], [], floor_per_hour=4.0)
        assert clears(self._vel(4), bar)

    def test_unknown_favourites_never_clears(self):
        # Views alone must not trigger: they're noisy, and a like is the only
        # signal that says someone wants this specific thing.
        bar = build_bar([], [], floor_per_hour=4.0)
        vel = velocity(
            [Sample(NOW - 3600, None, 0), Sample(NOW, None, 5000)], now=NOW,
            window_min=61,
        )
        assert not clears(vel, bar)

    def test_none_velocity_never_clears(self):
        assert not clears(None, build_bar([], []))


class TestMinimumGain:
    """A rate is a gain over a span, so a short span inflates a tiny gain.

    One like a minute after we first see a listing reads as 60 likes/hour and
    clears any sane floor. Requiring real likes is what makes the trigger robust:
    no arithmetic can turn one like into a crowd.
    """

    def _vel(self, favourites, minutes):
        return velocity(
            [Sample(NOW - minutes * 60, 0), Sample(NOW, favourites)],
            now=NOW, window_min=minutes + 1,
        )

    def test_one_like_over_a_short_span_does_not_clear(self):
        vel = self._vel(1, 1)
        assert vel.fav_per_min * 60 == 60.0        # "60 likes/hour"
        assert not clears(vel, build_bar([], [], floor_per_hour=4.0), min_gain=3)

    def test_enough_likes_over_a_short_span_does_clear(self):
        assert clears(self._vel(5, 1), build_bar([], [], floor_per_hour=4.0),
                      min_gain=3)

    def test_gain_alone_is_not_enough_the_rate_must_also_clear(self):
        # 3 likes spread over 10 hours is not hot, however many likes it is.
        vel = self._vel(3, 600)
        assert vel.fav_gain == 3
        assert not clears(vel, build_bar([], [], floor_per_hour=4.0), min_gain=3)

    def test_gain_is_recorded_on_the_velocity(self):
        assert self._vel(7, 20).fav_gain == 7

    def test_backwards_counter_gains_nothing(self):
        vel = velocity([Sample(NOW - 600, 9), Sample(NOW, 4)], now=NOW)
        assert vel.fav_gain == 0

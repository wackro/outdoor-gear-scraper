"""The velocity maths — the one piece of this system that fails silently.

A wrong rate doesn't crash, it just quietly stops alerting (or floods you), which
is why these cases are worth pinning down.
"""
from src.hot.hotness import MIN_SPAN_MIN, Sample, heat, pct_rank, velocity

NOW = 1_000_000.0


def s(minutes_ago: float, favourites=None, views=None) -> Sample:
    return Sample(NOW - minutes_ago * 60, favourites, views)


class TestVelocity:
    def test_measures_gain_between_samples(self):
        vel = velocity([s(20, 0, 0), s(0, 10, 100)], now=NOW)
        assert vel.source == "delta"
        assert vel.span_min == 20
        assert vel.fav_per_min == 0.5      # 10 likes over 20 minutes
        assert vel.view_per_min == 5.0

    def test_anchors_to_oldest_sample_inside_the_window(self):
        # A poll-to-poll delta is almost always zero at a 60s cadence, so the
        # anchor must be the oldest sample still in the window, not the previous.
        samples = [s(25, 0), s(10, 8), s(0, 10)]
        assert velocity(samples, now=NOW, window_min=30).span_min == 25

    def test_ignores_samples_older_than_the_window(self):
        # A listing hot two hours ago is not hot now.
        samples = [s(200, 0), s(20, 40), s(0, 41)]
        vel = velocity(samples, now=NOW, window_min=30)
        assert vel.span_min == 20
        assert vel.fav_per_min == 0.05      # 1 like in 20 min, not 41 in 200

    def test_short_span_is_damped_not_amplified(self):
        # Two likes six seconds apart is not 1200 likes/hour.
        vel = velocity([s(0.1, 0), s(0, 2)], now=NOW)
        assert vel.fav_per_min == 2 / MIN_SPAN_MIN

    def test_counter_going_backwards_floors_at_zero(self):
        # Un-favouriting must not produce negative hotness.
        vel = velocity([s(10, 5), s(0, 3)], now=NOW)
        assert vel.fav_per_min == 0.0

    def test_unknown_counts_stay_unknown(self):
        # Vinted omits these for some listings; a missing count is not zero likes.
        vel = velocity([s(10, None, 5), s(0, None, 50)], now=NOW)
        assert vel.fav_per_min is None
        assert vel.view_per_min == 4.5

    def test_single_sample_bootstraps_from_listing_age(self):
        vel = velocity([s(0, 12)], now=NOW, listed_ts=int(NOW - 30 * 60))
        assert vel.source == "bootstrap"
        assert not vel.measured
        assert vel.fav_per_min == 0.4       # 12 likes over its 30-minute life

    def test_single_sample_without_an_age_is_unscoreable(self):
        assert velocity([s(0, 12)], now=NOW, listed_ts=None) is None

    def test_no_samples(self):
        assert velocity([], now=NOW) is None

    def test_future_timestamp_does_not_produce_a_negative_rate(self):
        assert velocity([s(0, 5)], now=NOW, listed_ts=int(NOW + 600)) is None


class TestPctRank:
    def test_empty_population_ranks_bottom(self):
        # Cold start must not rank everything as hot.
        assert pct_rank(5.0, []) == 0.0

    def test_ranks_within_population(self):
        assert pct_rank(3.0, [1.0, 2.0, 3.0, 4.0]) == 0.75

    def test_above_everything_ranks_top(self):
        assert pct_rank(99.0, [1.0, 2.0, 3.0]) == 1.0


class TestHeat:
    def _vel(self, fav, view):
        return velocity(
            [Sample(NOW - 600, 0, 0), Sample(NOW, fav, view)], now=NOW
        )

    def test_combines_both_signals_by_weight(self):
        population = [float(i) / 10 for i in range(100)]
        score = heat(
            self._vel(10, 10),
            fav_population=population, view_population=population,
            fav_weight=0.7, view_weight=0.3,
        )
        assert 0.0 <= score <= 1.0

    def test_missing_view_count_renormalises_rather_than_penalises(self):
        # An item with no view data should be judged on its likes alone, not
        # capped at 70% of its true heat by a gap in Vinted's response.
        population = [0.0] * 99 + [1.0]   # our 6.0/min rate beats all of it
        both = heat(self._vel(60, 60), fav_population=population,
                    view_population=population)
        fav_only = heat(self._vel(60, None), fav_population=population,
                        view_population=population)
        assert fav_only == both == 1.0

    def test_no_usable_signal_scores_zero(self):
        assert heat(self._vel(None, None), fav_population=[1.0],
                    view_population=[1.0]) == 0.0

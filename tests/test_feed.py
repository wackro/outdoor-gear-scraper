"""The JSON feed behind the website."""
import time

import pytest

from src.config import load_config
from src.hot.alerts import BaselineLookup
from src.hot.feed import build_feed, feed_changed
from src.hot.state import HotState
from src.hot.thresholds import build_bar
from src.vinted.models import VintedItem

NOW = time.time()
BAR = build_bar([], [], floor_per_hour=4.0)
NO_BASELINES = BaselineLookup({})


@pytest.fixture
def state(tmp_path):
    with HotState(tmp_path / "hot.db") as hot:
        yield hot


@pytest.fixture
def config():
    return load_config("config/config.example.yaml")


def item(item_id=1, *, favourites=0, price=30.0, size="M", condition="Very good",
         listed_minutes_ago=30.0, brand_title="Rab"):
    return VintedItem(
        id=item_id, title="Beta AR", price=price, currency="GBP",
        brand_title=brand_title, size=size, condition=condition,
        url=f"u/{item_id}", image_url="img", favourite_count=favourites,
        view_count=favourites * 10, listed_ts=int(NOW - listed_minutes_ago * 60),
    )


def track(state, it, *, minutes_ago):
    state.record(it, brand="rab", category="men_jackets", gender="men",
                 garment_type="clothes", now=NOW - minutes_ago * 60)
    state.commit()


def warm(state, item_id=1, favourites=20, **kwargs):
    track(state, item(item_id, favourites=0, **kwargs), minutes_ago=20)
    track(state, item(item_id, favourites=favourites, **kwargs), minutes_ago=0)


class TestBuildFeed:
    def test_includes_everything_tracked_not_just_alerts(self, state, config):
        # The point of the page: seeing near-misses is how you judge the bar.
        warm(state, 1, favourites=40)
        warm(state, 2, favourites=1)
        feed = build_feed(state, config, BAR, NO_BASELINES, now=NOW)
        assert {i["id"] for i in feed["items"]} == {1, 2}
        assert feed["counts"]["alerted"] == 0

    def test_ranked_hottest_first(self, state, config):
        warm(state, 1, favourites=5)
        warm(state, 2, favourites=90)
        population = [float(i) / 20 for i in range(200)]
        bar = build_bar(population, population, min_population=10)
        feed = build_feed(state, config, bar, NO_BASELINES, now=NOW)
        assert [i["id"] for i in feed["items"]] == [2, 1]

    def test_alerted_items_are_flagged(self, state, config):
        warm(state, 1)
        state.mark_alerted(1, heat=0.9, fav_rate=60, view_rate=600,
                           price=30.0, baseline=None)
        state.commit()
        feed = build_feed(state, config, BAR, NO_BASELINES, now=NOW)
        assert feed["items"][0]["alerted"] is True
        assert feed["counts"]["alerted"] == 1

    def test_sold_items_carry_their_time_to_sell(self, state, config):
        warm(state, 1)
        state.mark_gone([1], NOW)
        state.commit()
        feed = build_feed(state, config, BAR, NO_BASELINES, now=NOW)
        entry = feed["items"][0]
        assert entry["sold"] is True
        assert entry["seconds_to_sell"] == pytest.approx(1800, abs=2)
        assert feed["median_seconds_to_sell"] == pytest.approx(1800, abs=2)

    def test_same_wearability_gates_as_alerts(self, state, config):
        # The site must never show what your phone would have refused to.
        warm(state, 1, size="XXS")
        warm(state, 2, condition="Satisfactory")
        assert build_feed(state, config, BAR, NO_BASELINES, now=NOW)["items"] == []

    def test_uses_the_display_brand_name(self, state, config):
        warm(state, 1, brand_title="The North Face")
        assert build_feed(state, config, BAR, NO_BASELINES,
                          now=NOW)["items"][0]["brand_title"] == "The North Face"

    def test_discount_computed_against_the_baseline(self, state, config):
        warm(state, 1, price=25.0)
        baselines = BaselineLookup({("rab", "men_jackets"): (100.0, 50)})
        entry = build_feed(state, config, BAR, baselines, now=NOW)["items"][0]
        assert entry["discount_pct"] == pytest.approx(0.75)

    def test_respects_the_limit(self, state, config):
        for i in range(10):
            warm(state, i + 1, favourites=i)
        assert len(build_feed(state, config, BAR, NO_BASELINES,
                              now=NOW, limit=4)["items"]) == 4

    def test_reports_the_live_bar(self, state, config):
        feed = build_feed(state, config, BAR, NO_BASELINES, now=NOW)
        assert feed["bar"]["favourites_per_hour"] == pytest.approx(4.0)
        assert feed["bar"]["adaptive"] is False

    def test_empty_state_is_a_valid_feed(self, state, config):
        feed = build_feed(state, config, BAR, NO_BASELINES, now=NOW)
        assert feed["items"] == []
        assert feed["counts"]["tracked"] == 0
        assert feed["median_seconds_to_sell"] is None


class TestFeedChanged:
    def _feed(self, items):
        return {"items": items}

    def test_first_feed_always_counts_as_changed(self):
        assert feed_changed(None, self._feed([]))

    def test_identical_feed_is_unchanged(self):
        f = self._feed([{"id": 1, "alerted": False, "sold": False, "heat": 0.5}])
        assert not feed_changed(f, self._feed(list(f["items"])))

    def test_tiny_heat_jitter_does_not_trigger_a_publish(self):
        # Rates wobble every cycle as the anchor sample moves; without rounding
        # this would push a commit every single minute.
        a = self._feed([{"id": 1, "alerted": False, "sold": False, "heat": 0.5000}])
        b = self._feed([{"id": 1, "alerted": False, "sold": False, "heat": 0.5004}])
        assert not feed_changed(a, b)

    def test_a_real_heat_move_does(self):
        a = self._feed([{"id": 1, "alerted": False, "sold": False, "heat": 0.50}])
        b = self._feed([{"id": 1, "alerted": False, "sold": False, "heat": 0.72}])
        assert feed_changed(a, b)

    def test_a_new_listing_does(self):
        a = self._feed([{"id": 1, "alerted": False, "sold": False, "heat": 0.5}])
        b = self._feed([{"id": 1, "alerted": False, "sold": False, "heat": 0.5},
                        {"id": 2, "alerted": False, "sold": False, "heat": 0.4}])
        assert feed_changed(a, b)

    def test_a_sale_does(self):
        a = self._feed([{"id": 1, "alerted": False, "sold": False, "heat": 0.5}])
        b = self._feed([{"id": 1, "alerted": False, "sold": True, "heat": 0.5}])
        assert feed_changed(a, b)

    def test_an_alert_does(self):
        a = self._feed([{"id": 1, "alerted": False, "sold": False, "heat": 0.5}])
        b = self._feed([{"id": 1, "alerted": True, "sold": False, "heat": 0.5}])
        assert feed_changed(a, b)

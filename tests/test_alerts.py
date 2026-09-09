"""End-to-end alert rules against a real (temporary) hot-state database."""
import time

import pytest

from src.config import load_config
from src.hot.alerts import BaselineLookup, evaluate, population_rates
from src.hot.state import HotState
from src.hot.thresholds import build_bar
from src.vinted.models import VintedItem

NOW = time.time()


@pytest.fixture
def state(tmp_path):
    with HotState(tmp_path / "hot.db") as hot:
        yield hot


@pytest.fixture
def config():
    return load_config("config/config.example.yaml")


def item(item_id=1, *, price=30.0, favourites=0, views=0, size="M",
         condition="Very good", listed_minutes_ago=30.0):
    return VintedItem(
        id=item_id, title="Beta AR", price=price, currency="GBP",
        brand_title="Rab", size=size, condition=condition,
        url=f"https://vinted.co.uk/items/{item_id}", image_url="img",
        favourite_count=favourites, view_count=views,
        listed_ts=int(NOW - listed_minutes_ago * 60),
    )


def track(state, it, *, minutes_ago, garment_type="clothes", gender="men",
          catalog_id=2052):
    """Record one observation of a listing at a point in the past."""
    state.record(it, brand="rab", catalog_id=catalog_id, gender=gender,
                 garment_type=garment_type, now=NOW - minutes_ago * 60)
    state.commit()


def hot_item(state, item_id=1, **kwargs):
    """A listing that gained 20 likes over the last 20 minutes."""
    track(state, item(item_id, favourites=0, views=0, **kwargs), minutes_ago=20)
    track(state, item(item_id, favourites=20, views=200, **kwargs), minutes_ago=0)


BAR = build_bar([], [], floor_per_hour=4.0)
NO_BASELINES = BaselineLookup({})


class TestEvaluate:
    def test_hot_listing_alerts(self, state, config):
        hot_item(state)
        alerts = evaluate(state, config, BAR, NO_BASELINES, now=NOW)
        assert len(alerts) == 1
        assert alerts[0].item_id == 1
        assert alerts[0].favourites == 20
        assert alerts[0].measured

    def test_cold_listing_does_not(self, state, config):
        track(state, item(favourites=0), minutes_ago=20)
        track(state, item(favourites=1), minutes_ago=0)
        assert evaluate(state, config, BAR, NO_BASELINES, now=NOW) == []

    def test_wrong_size_is_filtered(self, state, config):
        # Reuses the same allow-list the website uses, so the two can't disagree.
        hot_item(state, size="XXS")
        assert evaluate(state, config, BAR, NO_BASELINES, now=NOW) == []

    def test_poor_condition_is_filtered(self, state, config):
        hot_item(state, condition="Satisfactory")
        assert evaluate(state, config, BAR, NO_BASELINES, now=NOW) == []

    def test_too_young_to_judge(self, state, config):
        # Dividing a like count by a near-zero age manufactures huge rates.
        hot_item(state, listed_minutes_ago=1.0)
        assert evaluate(state, config, BAR, NO_BASELINES, now=NOW) == []

    def test_too_old_to_chase(self, state, config):
        hot_item(state, listed_minutes_ago=600.0)
        assert evaluate(state, config, BAR, NO_BASELINES, now=NOW) == []

    def test_over_the_price_ceiling(self, state, config):
        hot_item(state, price=config.alerts.max_price + 1)
        assert evaluate(state, config, BAR, NO_BASELINES, now=NOW) == []

    def test_dedup_suppresses_a_repeat(self, state, config):
        hot_item(state)
        assert len(evaluate(state, config, BAR, NO_BASELINES, now=NOW)) == 1
        state.mark_alerted(1, heat=1.0, fav_rate=60, view_rate=600,
                           price=30.0, baseline=None)
        state.commit()
        assert evaluate(state, config, BAR, NO_BASELINES, now=NOW) == []

    def test_bootstrap_estimates_are_off_by_default(self, state, config):
        # One sighting, no delta — an age-derived guess, and the main source of
        # false alarms, so it must not fire unless explicitly enabled.
        track(state, item(favourites=50), minutes_ago=0)
        assert evaluate(state, config, BAR, NO_BASELINES, now=NOW) == []

    def test_results_are_ordered_hottest_first(self, state, config):
        # So that if the burst cap bites, it drops the weakest alerts.
        hot_item(state, item_id=1)
        track(state, item(2, favourites=0), minutes_ago=20)
        track(state, item(2, favourites=6), minutes_ago=0)
        population = [0.0] * 199 + [0.2]
        bar = build_bar(population, population, floor_per_hour=4.0,
                        min_population=200)
        alerts = evaluate(state, config, bar, NO_BASELINES, now=NOW)
        assert [a.item_id for a in alerts] == [1, 2]


class TestPriceVeto:
    def test_hot_but_full_price_is_rejected(self, state, config):
        # Popular at a fair price is not a bargain.
        hot_item(state, price=100.0)
        baselines = BaselineLookup({("rab", 2052): (100.0, 50)})
        assert evaluate(state, config, BAR, baselines, now=NOW) == []

    def test_hot_and_cheap_passes_with_a_discount(self, state, config):
        hot_item(state, price=30.0)
        baselines = BaselineLookup({("rab", 2052): (100.0, 50)})
        alerts = evaluate(state, config, BAR, baselines, now=NOW)
        assert len(alerts) == 1
        assert alerts[0].discount_pct == pytest.approx(0.7)

    def test_missing_baseline_does_not_block(self, state, config):
        # The whole point of hotness: rare brands with no price history are
        # exactly the ones the old price-only detector could never flag.
        hot_item(state, price=100.0)
        alerts = evaluate(state, config, BAR, NO_BASELINES, now=NOW)
        assert len(alerts) == 1
        assert alerts[0].baseline is None

    def test_urgent_only_when_hot_and_clearly_underpriced(self, state, config):
        hot_item(state, item_id=1, price=30.0)      # 70% off -> urgent
        hot_item(state, item_id=2, price=85.0)      # 15% off -> not urgent
        baselines = BaselineLookup({("rab", 2052): (100.0, 50)})
        by_id = {a.item_id: a for a in
                 evaluate(state, config, BAR, baselines, now=NOW)}
        assert by_id[1].urgent
        assert not by_id[2].urgent


class TestPopulationRates:
    def test_collects_measured_rates_only(self, state, config):
        hot_item(state, item_id=1)
        track(state, item(2, favourites=50), minutes_ago=0)   # bootstrap only
        fav_rates, view_rates = population_rates(state, config, now=NOW)
        assert fav_rates == [1.0]        # 20 likes / 20 min, from item 1 alone
        assert view_rates == [10.0]

    def test_excludes_listings_past_their_first_hour(self, state, config):
        # An old listing's rate has decayed to nothing; including it would drag
        # the distribution down and lower the bar for everyone.
        hot_item(state, listed_minutes_ago=300.0)
        assert population_rates(state, config, now=NOW) == ([], [])

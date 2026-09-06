"""The poller's disposable state file."""
import time

import pytest

from src.hot.state import HotState
from src.vinted.models import VintedItem

NOW = time.time()


def item(item_id=1, favourites=3, views=30):
    return VintedItem(
        id=item_id, title="t", price=10.0, currency="GBP", brand_title="Rab",
        size="M", condition="Good", url="u", image_url="i",
        favourite_count=favourites, view_count=views, listed_ts=int(NOW - 600),
    )


def record(state, it, now):
    state.record(it, brand="rab", category="men_jackets", gender="men",
                 garment_type="clothes", now=now)
    state.commit()


def test_samples_accumulate_per_item(tmp_path):
    with HotState(tmp_path / "h.db") as state:
        record(state, item(favourites=1), NOW - 600)
        record(state, item(favourites=9), NOW)
        samples = state.samples_by_item(NOW - 3600)[1]
        assert [s.favourites for s in samples] == [1, 9]


def test_repeated_poll_in_the_same_second_is_a_noop(tmp_path):
    with HotState(tmp_path / "h.db") as state:
        record(state, item(), NOW)
        record(state, item(), NOW)
        assert len(state.samples_by_item(NOW - 60)[1]) == 1


def test_first_seen_survives_upserts(tmp_path):
    # It's the fallback age signal when Vinted gives us no photo timestamp.
    with HotState(tmp_path / "h.db") as state:
        record(state, item(), NOW - 3600)
        record(state, item(), NOW)
        assert state.meta_for([1])[1]["first_seen"] == NOW - 3600


def test_age_prefers_the_listing_timestamp(tmp_path):
    with HotState(tmp_path / "h.db") as state:
        record(state, item(), NOW)
        meta = state.meta_for([1])[1]
        # listed_ts is stored as whole seconds, hence the tolerance.
        assert state.age_seconds(meta, NOW) == pytest.approx(600, abs=1)


def test_age_falls_back_to_first_sighting(tmp_path):
    with HotState(tmp_path / "h.db") as state:
        it = VintedItem(id=2, title="t", price=1.0, currency="GBP",
                        brand_title="Rab", size="M", condition="Good",
                        url="u", image_url="i", listed_ts=None)
        record(state, it, NOW - 300)
        assert state.age_seconds(state.meta_for([2])[2], NOW) == 300


def test_dedup_roundtrip(tmp_path):
    with HotState(tmp_path / "h.db") as state:
        assert not state.has_alerted(1)
        state.mark_alerted(1, heat=1.0, fav_rate=60, view_rate=None,
                           price=10.0, baseline=None)
        assert state.has_alerted(1)


def test_seeding_dedup_after_a_cache_miss(tmp_path):
    # Without this a lost cache re-pushes everything still listed.
    with HotState(tmp_path / "h.db") as state:
        state.seed_alerted([7, 8, 9])
        assert all(state.has_alerted(i) for i in (7, 8, 9))


def test_prune_drops_old_samples_but_keeps_dedup(tmp_path):
    with HotState(tmp_path / "h.db") as state:
        record(state, item(), NOW - 86400)
        state.mark_alerted(1, heat=1.0, fav_rate=1, view_rate=1,
                           price=1.0, baseline=None)
        state.commit()
        samples, _ = state.prune(sample_hours=6, alert_hours=72, now=NOW)
        assert samples == 1
        assert state.has_alerted(1)     # dedup outlives the samples

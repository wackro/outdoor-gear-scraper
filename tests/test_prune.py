"""Pruning dead listings, which is the only thing bounding the database file.

The file is committed to git, and git hard-fails above 100 MB. Before this
existed the database grew ~2 MB a day with nothing ever deleted -- `items` was
48 MB of an 81 MB file -- so the exclusions below are the difference between
bounded growth and a repository that stops accepting pushes.
"""
import time

import pytest

from src.storage.db import Database
from src.vinted.models import VintedItem


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "v.db") as database:
        yield database


def item(n):
    return VintedItem(
        id=n, title=f"Item {n}", price=40.0, currency="GBP", brand_title="Rab",
        size="M", condition="Very good", url=f"https://vinted.co.uk/items/{n}",
        image_url="img", favourite_count=1, view_count=10,
        listed_ts=int(time.time() - 3600),
    )


def store(db, n, *, active=1, days_ago=0):
    """Add an item, then age it -- upsert_item always stamps `now`."""
    db.upsert_item(item(n), brand="rab", category="men_jackets", catalog_id=2052,
                   gender="men", garment_type="clothes")
    db.conn.execute(
        "UPDATE items SET active = ?, last_seen = datetime('now', ?) WHERE id = ?",
        (active, f"-{days_ago} day", n),
    )
    db.commit()


def alert(db, n):
    db.conn.execute(
        "INSERT INTO alerted (item_id, alerted_at, heat, price) VALUES (?,?,?,?)",
        (n, "2026-09-06T10:00:00+00:00", 0.9, 40.0),
    )
    db.commit()


def ids(db):
    return {r[0] for r in db.conn.execute("SELECT id FROM items")}


class TestPruneItems:
    def test_a_gone_listing_past_the_grace_period_is_deleted(self, db):
        store(db, 1, active=0, days_ago=30)
        assert db.prune_items(7) == 1
        assert ids(db) == set()

    def test_the_grace_period_is_respected(self, db):
        """A relisted item can come back; inside the window we still know it."""
        store(db, 1, active=0, days_ago=3)
        assert db.prune_items(7) == 0
        assert ids(db) == {1}

    def test_an_active_listing_is_never_pruned(self, db):
        store(db, 1, active=1, days_ago=365)
        assert db.prune_items(7) == 0
        assert ids(db) == {1}

    def test_an_alerted_listing_survives_even_when_long_gone(self, db):
        """This is the site's sold / time-to-sell history.

        The page builds it from `alerted JOIN items`, and alerted listings go
        inactive quickly -- being sold is the point -- so without this exclusion
        the prune would delete precisely the rows the page is there to show.
        """
        store(db, 1, active=0, days_ago=90)
        alert(db, 1)
        assert db.prune_items(7) == 0
        assert ids(db) == {1}

    def test_price_history_survives_the_prune(self, db):
        """Baselines must not care. They read price_observations, which carries
        its own brand and price and never joins items."""
        store(db, 1, active=0, days_ago=30)
        db.add_observation(item(1), brand="rab", category="men_jackets")
        db.commit()

        db.prune_items(7)
        db.commit()

        observations = db.observations_within(365)
        assert len(observations) == 1
        assert observations[0].brand == "rab"
        assert observations[0].price == 40.0

    def test_pruning_before_deals_are_rebuilt_is_a_foreign_key_error(self, db):
        """Why prune_items runs after rebuild_deals, not next to the other two.

        `deals.item_id` references `items(id)` and foreign keys are on. Until
        `replace_deals` clears it, the table still holds the previous run's rows,
        which can point at an item this run has just marked inactive. Ordering it
        wrongly would pass every test and every review, then fail on live data.
        """
        store(db, 1, active=0, days_ago=30)
        db.replace_deals([{
            "item_id": 1, "brand": "rab", "category": "men_jackets",
            "price": 40.0, "baseline": 100.0, "baseline_src": "history",
            "discount_pct": 0.6, "deal_score": 3.0,
        }])
        db.commit()

        with pytest.raises(Exception, match="FOREIGN KEY"):
            db.prune_items(7)
            db.commit()

    def test_rebuilding_deals_first_makes_the_prune_safe(self, db):
        store(db, 1, active=0, days_ago=30)
        db.replace_deals([{
            "item_id": 1, "brand": "rab", "category": "men_jackets",
            "price": 40.0, "baseline": 100.0, "baseline_src": "history",
            "discount_pct": 0.6, "deal_score": 3.0,
        }])
        db.commit()

        db.replace_deals([])          # what rebuild_deals does with no active items
        assert db.prune_items(7) == 1
        db.commit()
        assert ids(db) == set()


class TestVacuum:
    def test_it_reclaims_space_a_delete_alone_does_not(self, db):
        """Deleting rows leaves the pages allocated, so the file never shrinks.

        This is the whole reason the prune has to be followed by a repack: the
        constraint is the size of the file on disk, not the row count.
        """
        for n in range(2000):
            store(db, n, active=0, days_ago=30)

        before = db.path.stat().st_size
        assert db.prune_items(7) == 2000
        db.commit()
        assert db.path.stat().st_size == before, "a delete should not shrink the file"

        reclaimed = db.vacuum()
        assert reclaimed > 0
        assert db.path.stat().st_size < before

    def test_it_is_safe_on_a_database_with_nothing_to_reclaim(self, db):
        store(db, 1)
        assert db.vacuum() >= 0

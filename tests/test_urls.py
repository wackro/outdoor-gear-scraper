"""Listing links, end to end through the stores.

The page assigns an href only when the URL passes an http(s) whitelist, so a
relative path renders a card that looks perfect and does nothing when tapped.
Resolving it at parse time fixes the feed, the database, the site fallback and
the push notification at once -- but only for listings seen *after* the fix, so
what is pinned here is that rows written before it get repaired too.
"""
import sqlite3
import time

import pytest

from src.storage.db import SCHEMA_VERSION, SITE_BASE_URL, Database
from src.vinted.models import VintedItem


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "v.db") as database:
        yield database


def item(n, url):
    return VintedItem(
        id=n, title=f"Item {n}", price=40.0, currency="GBP", brand_title="Rab",
        size="M", condition="Very good", url=url, image_url="img",
        favourite_count=1, view_count=10, listed_ts=int(time.time() - 3600),
    )


def store(db, n, url):
    db.upsert_item(item(n, url), brand="rab", catalog_id=2052,
                   gender="men", garment_type="clothes")
    db.commit()


def url_of(db, n):
    return db.conn.execute("SELECT url FROM items WHERE id = ?", (n,)).fetchone()[0]


class TestResighting:
    """`upsert_item` used to freeze the URL at first sight."""

    def test_a_re_sighting_repairs_a_bad_url(self, db):
        store(db, 1, "/items/1-a-jacket")
        assert url_of(db, 1) == "/items/1-a-jacket"

        store(db, 1, "https://www.vinted.co.uk/items/1-a-jacket")
        assert url_of(db, 1) == "https://www.vinted.co.uk/items/1-a-jacket"

    def test_the_poller_store_repairs_it_too(self, tmp_path):
        """Both stores feed the page -- the live feed from one, the fallback
        from the other -- so a fix that reached only one would leave the cards
        working until the feed went stale, then break them again."""
        from src.hot.state import HotState
        state = HotState(tmp_path / "hot.db")
        now = time.time()
        common = dict(brand="rab", catalog_id=2052, gender="men",
                      garment_type="clothes")
        state.record(item(1, "/items/1-a"), now=now, **common)
        state.record(item(1, "https://www.vinted.co.uk/items/1-a"),
                     now=now + 1, **common)
        row = state.conn.execute(
            "SELECT url FROM item_meta WHERE item_id = 1").fetchone()
        assert row["url"] == "https://www.vinted.co.uk/items/1-a"


class TestMigration:
    """For rows that went inactive while broken and are never seen again."""

    def test_bare_paths_are_made_absolute(self, db, tmp_path):
        store(db, 1, "/items/1-a-jacket")
        store(db, 2, "/items/2-b-jacket")
        db.conn.execute("PRAGMA user_version = 2")   # pretend it predates the fix
        db.commit()
        db.close()

        with Database(tmp_path / "v.db") as reopened:
            assert url_of(reopened, 1) == f"{SITE_BASE_URL}/items/1-a-jacket"
            assert url_of(reopened, 2) == f"{SITE_BASE_URL}/items/2-b-jacket"
            assert reopened._version() == SCHEMA_VERSION

    def test_absolute_urls_are_left_alone(self, db, tmp_path):
        store(db, 1, "https://www.vinted.co.uk/items/1-a")
        db.conn.execute("PRAGMA user_version = 2")
        db.commit()
        db.close()

        with Database(tmp_path / "v.db") as reopened:
            assert url_of(reopened, 1) == "https://www.vinted.co.uk/items/1-a"

    def test_it_does_not_run_twice(self, db, tmp_path):
        """Running it again would produce a doubled prefix, so the version gate
        is the thing being tested, not the UPDATE."""
        store(db, 1, "/items/1-a")
        db.conn.execute("PRAGMA user_version = 2")
        db.commit()
        db.close()

        for _ in range(3):
            with Database(tmp_path / "v.db") as reopened:
                assert url_of(reopened, 1) == f"{SITE_BASE_URL}/items/1-a"


class TestTheWholeChain:
    def test_a_service_response_ends_up_clickable_in_the_database(self, db):
        """The shape the live service actually returns, start to finish."""
        parsed = VintedItem.from_json(
            {"id": 7, "title": "Alpha SV",
             "price": {"amount": "160.00", "currency_code": "GBP"},
             "url": "/items/7-alpha-sv",
             "item_box": {"first_line": "Arc'teryx", "second_line": "L · Very good"}},
            base_url="https://www.vinted.co.uk",
        )
        db.upsert_item(parsed, brand="arcteryx", catalog_id=2052,
                       gender="men", garment_type="clothes")
        db.commit()
        assert url_of(db, 7) == "https://www.vinted.co.uk/items/7-alpha-sv"

"""Folding the poller's alert log into the committed database.

This is what makes alert dedup survive the Actions cache being evicted: without
it a lost cache re-pushes every listing we already notified on.
"""
from src.hot.state import HotState
from src.storage.db import Database


def test_merges_alerts_and_survives_a_second_run(tmp_path):
    hot_path = tmp_path / "hot.db"
    with HotState(hot_path) as hot:
        for item_id in (11, 22):
            hot.mark_alerted(item_id, heat=0.9, fav_rate=60.0, view_rate=600.0,
                             price=30.0, baseline=100.0)
        hot.commit()

    with Database(tmp_path / "vinted.db") as db:
        assert db.merge_alerts(hot_path) == 2
        assert sorted(db.alerted_ids()) == [11, 22]
        # Idempotent: merging again must not duplicate or error.
        db.merge_alerts(hot_path)
        assert sorted(db.alerted_ids()) == [11, 22]
        # And the connection is still usable afterwards — the DETACH succeeded.
        db.conn.execute("SELECT count(*) FROM items").fetchone()


def test_missing_hot_db_is_not_an_error(tmp_path):
    # The daily job runs whether or not the poller has ever produced state.
    with Database(tmp_path / "vinted.db") as db:
        assert db.merge_alerts(tmp_path / "nope.db") == 0


def test_carries_listing_metadata_the_daily_scrape_never_saw(tmp_path):
    """A listing that appeared and sold between daily scrapes must still render.

    `items` is otherwise only populated by the daily scrape, so without this the
    fastest-selling listings -- the ones this whole system exists to catch --
    would have no title, photo or URL to show.
    """
    import time

    from src.vinted.models import VintedItem

    now = time.time()
    hot_path = tmp_path / "hot.db"
    with HotState(hot_path) as hot:
        listing = VintedItem(
            id=777, title="Beta AR jacket", price=38.0, currency="GBP",
            brand_title="The North Face", size="M", condition="Very good",
            url="https://vinted.co.uk/items/777", image_url="http://img/1.jpg",
            favourite_count=20, view_count=300, listed_ts=int(now - 1800),
        )
        hot.record(listing, brand="the_north_face", category="men_jackets",
                   gender="men", garment_type="clothes", now=now - 1700)
        hot.mark_alerted(777, heat=0.97, fav_rate=40.0, view_rate=600.0,
                         price=38.0, baseline=150.0)
        hot.mark_gone([777], now)
        hot.commit()

    with Database(tmp_path / "vinted.db") as db:
        db.merge_alerts(hot_path)
        item = db.conn.execute(
            "SELECT title, brand_title, url, image_url, active FROM items WHERE id = 777"
        ).fetchone()
        assert item["title"] == "Beta AR jacket"
        assert item["brand_title"] == "The North Face"
        assert item["url"].endswith("/777")
        assert item["active"] == 0          # it sold

        alerted = db.conn.execute(
            "SELECT sold_at, seconds_to_sell FROM alerted WHERE item_id = 777"
        ).fetchone()
        assert alerted["sold_at"]
        assert alerted["seconds_to_sell"] == 1800


def test_a_scraped_row_is_never_overwritten_by_the_poller(tmp_path):
    """The daily scrape carries catalog_id and history the poller never sees."""
    import time

    from src.vinted.models import VintedItem

    now = time.time()
    hot_path = tmp_path / "hot.db"
    with HotState(hot_path) as hot:
        hot.record(
            VintedItem(id=888, title="poller version", price=1.0, currency="GBP",
                       brand_title="Rab", size="M", condition="Good", url="u",
                       image_url="i"),
            brand="rab", category="men_jackets", gender="men",
            garment_type="clothes", now=now,
        )
        hot.mark_alerted(888, heat=0.5, fav_rate=1, view_rate=1, price=1.0,
                         baseline=None)
        hot.commit()

    with Database(tmp_path / "vinted.db") as db:
        db.upsert_item(
            VintedItem(id=888, title="scraped version", price=42.0, currency="GBP",
                       brand_title="Rab", size="M", condition="Good", url="u2",
                       image_url="i2"),
            brand="rab", category="men_jackets", catalog_id=2052,
            gender="men", garment_type="clothes",
        )
        db.commit()
        db.merge_alerts(hot_path)
        row = db.conn.execute(
            "SELECT title, catalog_id FROM items WHERE id = 888").fetchone()
        assert row["title"] == "scraped version"
        assert row["catalog_id"] == 2052


def test_read_only_open_does_not_migrate(tmp_path):
    """Read-only callers must not rewrite the database.

    Opening the committed 72MB file normally runs the additive migration and
    dirties it in git — which is a large binary diff to pay for merely reading.
    """
    path = tmp_path / "v.db"
    with Database(path) as db:
        db.commit()
    before = path.stat().st_mtime_ns

    with Database(f"file:{path}?mode=ro&immutable=1") as db:
        assert db.read_only is True
        assert db.conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    assert path.stat().st_mtime_ns == before

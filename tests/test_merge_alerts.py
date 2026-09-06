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

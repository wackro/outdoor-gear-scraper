"""Re-keying categories from a name string onto the Vinted catalog id.

The database predates `catalog_id`, so opening an old file has to backfill it.
Nothing here can be checked by eye afterwards -- a wrong id looks exactly like a
right one -- so the properties are pinned instead.
"""
import sqlite3

import pytest

from src.storage.db import HISTORICAL_CATALOG_IDS, SCHEMA_VERSION, Database


def old_shape(path, *, items=(), observations=()):
    """A database as it looked before either catalog_id column existed."""
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE items (
            id INTEGER PRIMARY KEY, brand TEXT NOT NULL, category TEXT NOT NULL,
            price REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'GBP',
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE price_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
            brand TEXT NOT NULL, category TEXT NOT NULL, price REAL NOT NULL,
            observed TEXT NOT NULL
        );
        """
    )
    raw.executemany(
        "INSERT INTO items (id, brand, category, price, first_seen, last_seen) "
        "VALUES (?, 'rab', ?, 40.0, '2026-08-01T00:00:00+00:00', "
        "'2026-08-01T00:00:00+00:00')",
        items,
    )
    raw.executemany(
        "INSERT INTO price_observations (item_id, brand, category, price, observed) "
        "VALUES (?, 'rab', ?, 40.0, '2026-08-01T00:00:00+00:00')",
        observations,
    )
    raw.commit()
    raw.close()
    return path


def rows(db, sql):
    # Database sets row_factory to sqlite3.Row, which never equals a tuple.
    return [tuple(r) for r in db.conn.execute(sql)]


class TestBackfill:
    def test_observations_get_the_id_their_name_meant(self, tmp_path):
        path = old_shape(tmp_path / "v.db",
                         items=[(1, "men_jackets")],
                         observations=[(1, "men_jackets")])
        with Database(path) as db:
            assert rows(db, "SELECT catalog_id FROM price_observations") == [(2052,)]

    def test_a_retired_category_still_resolves(self, tmp_path):
        """Its listings are long deleted, so only the frozen literal knows it.

        This is what stops the backfill being time-dependent: `women_jackets` is
        not in config.yaml and, once `prune_items` has run, not in `items`
        either -- but its price history is still here.
        """
        path = old_shape(tmp_path / "v.db", observations=[(1, "women_jackets")])
        with Database(path) as db:
            assert rows(db, "SELECT catalog_id FROM price_observations") == [(1908,)]

    def test_an_item_seen_in_two_categories_keeps_both(self, tmp_path):
        """The trap. `items.category` records only where a listing was *first*
        seen, because upsert_item never updates it on conflict. Backfilling the
        observations by joining item_id would rewrite the second one to match the
        first, moving that price into a category it was never observed in and
        shifting the baseline that decides what counts as a bargain.
        """
        path = old_shape(
            tmp_path / "v.db",
            items=[(1, "men_bags_&_backpacks")],
            observations=[(1, "men_bags_&_backpacks"), (1, "women_bags")],
        )
        with Database(path) as db:
            got = rows(db, "SELECT catalog_id FROM price_observations "
                           "ORDER BY catalog_id")
        # 19 is women_bags, 94 is men_bags_&_backpacks. A join on item_id would
        # have produced [(94,), (94,)] -- `items` only knows the first sighting.
        assert got == [(19,), (94,)]

    def test_items_missing_an_id_are_filled_in(self, tmp_path):
        """merge_alerts inserts poller rows without a catalog_id."""
        path = old_shape(tmp_path / "v.db", items=[(1, "men_shoes")])
        with Database(path) as db:
            assert rows(db, "SELECT catalog_id FROM items") == [(1231,)]

    def test_nothing_is_left_null(self, tmp_path):
        names = ["men_jackets", "women_shoes", "men_jeans", "men_trousers"]
        path = old_shape(
            tmp_path / "v.db",
            items=list(enumerate(names)),
            observations=list(enumerate(names)),
        )
        with Database(path) as db:
            for table in ("items", "price_observations"):
                assert rows(db, f"SELECT count(*) FROM {table} "
                                f"WHERE catalog_id IS NULL") == [(0,)]


class TestSafety:
    def test_an_unknown_category_fails_loudly(self, tmp_path):
        """Rather than backfilling NULL, or guessing.

        A wrong id merges one category's price history into another's baseline,
        which is invisible from the outside and unrecoverable afterwards.
        """
        path = old_shape(tmp_path / "v.db", observations=[(1, "men_snowshoes")])
        with pytest.raises(RuntimeError, match="No catalog id known"):
            Database(path)

    def test_a_disagreement_between_the_two_sources_fails_loudly(self, tmp_path):
        path = old_shape(tmp_path / "v.db")
        raw = sqlite3.connect(path)
        raw.execute("ALTER TABLE items ADD COLUMN catalog_id INTEGER")
        raw.execute("INSERT INTO items (id, brand, category, catalog_id, price, "
                    "first_seen, last_seen) VALUES (1, 'rab', 'men_jackets', 9999, "
                    "40.0, 'x', 'x')")
        raw.commit()
        raw.close()
        with pytest.raises(RuntimeError, match="One is wrong"):
            Database(path)

    def test_the_migration_runs_once(self, tmp_path):
        path = old_shape(tmp_path / "v.db", observations=[(1, "men_jackets")])
        with Database(path) as db:
            assert db._version() == SCHEMA_VERSION
        # Re-opening must not redo the work, and must not undo it either.
        with Database(path) as db:
            assert db._version() == SCHEMA_VERSION
            assert rows(db, "SELECT catalog_id FROM price_observations") == [(2052,)]

    def test_an_interrupted_migration_is_redone(self, tmp_path):
        """The version is bumped last, so a half-finished backfill re-runs."""
        path = old_shape(tmp_path / "v.db", observations=[(1, "men_jackets")])
        raw = sqlite3.connect(path)
        raw.execute("ALTER TABLE price_observations ADD COLUMN catalog_id INTEGER")
        raw.commit()                    # column added, backfill never happened
        raw.close()
        with Database(path) as db:
            assert rows(db, "SELECT catalog_id FROM price_observations") == [(2052,)]

    def test_a_read_only_open_does_not_migrate(self, tmp_path):
        path = old_shape(tmp_path / "v.db", observations=[(1, "men_jackets")])
        before = path.stat().st_mtime_ns
        with Database(f"file:{path}?mode=ro") as db:
            assert db.read_only is True
        assert path.stat().st_mtime_ns == before


class TestFrozenMap:
    def test_it_is_one_to_one(self, tmp_path):
        """Two names sharing an id would merge their histories; one name with two
        ids cannot happen in a dict, but the reverse can and is worth pinning."""
        ids = list(HISTORICAL_CATALOG_IDS.values())
        assert len(set(ids)) == len(ids)

    def test_every_configured_category_is_covered(self):
        """Not required by the backfill -- new categories resolve from `items` --
        but a config category missing here means a future prune could orphan it.
        """
        from src.config import load_config
        configured = {c.id for c in load_config("config/config.yaml").categories}
        assert configured <= set(HISTORICAL_CATALOG_IDS.values())

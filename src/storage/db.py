"""SQLite persistence: schema init, upserts, observations, baselines, deals."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..vinted.models import VintedItem

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "vinted.db"

SCHEMA_VERSION = 1

# Every category name this database has ever stored, and the Vinted catalog id it
# means. Frozen as a literal on purpose.
#
# Categories used to be keyed by this name; they are keyed by `catalog_id` now,
# and re-keying the history needs exactly these pairs. Deriving them from
# `items` at migration time very nearly works -- the mapping is 1:1 in both
# directions, verified across all 26 -- but it is quietly time-dependent: a
# retired category's listings all go inactive, `prune_items` then deletes them,
# and its mapping disappears with them. The backfill would write NULL and no
# test would notice. Written down, it cannot rot.
#
# Nine of these are retired (the women's categories, jeans, t-shirts) and are not
# in config.yaml at all, which is the other reason config cannot be the source.
HISTORICAL_CATALOG_IDS = {
    "men_backpacks": 246,
    "men_bags_&_backpacks": 94,
    "men_climbing_shoes": 2673,
    "men_fleece_jackets": 1858,
    "men_gilets": 2553,
    "men_hiking_boots": 2678,
    "men_hoodies": 267,
    "men_jackets": 2052,
    "men_jeans": 257,
    "men_jumpers_&_sweaters": 79,
    "men_puffer_jackets": 2536,
    "men_pullovers": 585,
    "men_raincoats": 1859,
    "men_running_shoes": 1453,
    "men_shoes": 1231,
    "men_ski_jackets": 2539,
    "men_tops_&_t-shirts": 76,
    "men_trousers": 34,
    "men_windbreakers": 2551,
    "women_bags": 19,
    "women_jackets": 1908,
    "women_jeans": 183,
    "women_jumpers_&_hoodies": 1917,
    "women_shoes": 16,
    "women_tops_&_t-shirts": 12,
    "women_trousers,_shorts_&_dungarees": 573,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class Observation:
    brand: str
    category: str
    price: float


class Database:
    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        # A `file:...?mode=ro` URI opens the database read-only, which callers
        # that only want to read (previews, reports) should use: the normal path
        # runs the additive migration and would rewrite the whole file.
        uri = isinstance(path, str) and path.startswith("file:")
        self.path = Path(path) if not uri else Path(str(path).split("?")[0][5:])
        self.read_only = uri and "mode=ro" in str(path)
        if not uri:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), uri=uri)
        self.conn.row_factory = sqlite3.Row
        if not self.read_only:
            self.conn.execute("PRAGMA foreign_keys = ON")
            self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text())
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (CREATE IF NOT
        EXISTS won't alter an existing table)."""
        have = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
        for col, decl in (
            ("gender", "TEXT"), ("garment_type", "TEXT"), ("condition", "TEXT"),
            # Only ever reached a database created after it was added to
            # schema.sql; older files never grew it, which is the gap the
            # catalog-id migration below then has to fill.
            ("catalog_id", "INTEGER"),
            # Attention counters, added for the hotness poller. Recorded on the
            # daily scrape too, so the site can show them and so there is a
            # historical record to calibrate the alert thresholds against.
            ("favourite_count", "INTEGER"), ("view_count", "INTEGER"),
            ("listed_ts", "INTEGER"),
        ):
            if col not in have:
                self.conn.execute(f"ALTER TABLE items ADD COLUMN {col} {decl}")

        have_alerted = {row["name"] for row in self.conn.execute("PRAGMA table_info(alerted)")}
        for col, decl in (("sold_at", "TEXT"), ("seconds_to_sell", "INTEGER")):
            if col not in have_alerted:
                self.conn.execute(f"ALTER TABLE alerted ADD COLUMN {col} {decl}")

        if self._version() < 1:
            self._key_categories_by_id()
            self._set_version(1)

    def _version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _set_version(self, version: int) -> None:
        # Not parameterisable; the value is ours, not user input.
        self.conn.execute(f"PRAGMA user_version = {int(version)}")

    def _catalog_id_map(self) -> dict[str, int]:
        """name -> catalog id, for backfilling history.

        `HISTORICAL_CATALOG_IDS` is the frozen record and wins where it applies.
        Anything this database has learned since is read out of `items`, so a
        category added between that literal being written and this migration
        running still resolves. Where both know a name they must agree.
        """
        derived = {
            row["category"]: row["catalog_id"]
            for row in self.conn.execute(
                "SELECT category, catalog_id FROM items "
                "WHERE catalog_id IS NOT NULL GROUP BY category"
            )
        }
        for name, catalog_id in HISTORICAL_CATALOG_IDS.items():
            if name in derived and derived[name] != catalog_id:
                raise RuntimeError(
                    f"Category {name!r} is id {derived[name]} in items but "
                    f"{catalog_id} in HISTORICAL_CATALOG_IDS. One is wrong; "
                    f"re-keying on either would corrupt that category's history."
                )
        return {**derived, **HISTORICAL_CATALOG_IDS}

    def _key_categories_by_id(self) -> None:
        """Give `price_observations` a catalog id, and fill the gaps in `items`.

        Every step is guarded or idempotent and the version is only bumped once
        they have all succeeded, so an interruption leaves a database this will
        simply redo -- which beats a transaction whose rollback journal would
        have to hold a rewrite of the largest table in the file.

        The backfill maps `price_observations.category`, and deliberately does
        **not** join `item_id` to `items.catalog_id`. That join looks obviously
        right and is wrong: `upsert_item` never updates `category` on conflict, so
        `items` records where a listing was *first* seen, while thousands of
        listings have been observed under more than one category. Joining would
        silently move those observations into a category they were never seen in,
        shifting the baselines that decide what counts as a bargain.
        """
        have = {row["name"] for row in self.conn.execute(
            "PRAGMA table_info(price_observations)")}
        if "catalog_id" not in have:
            self.conn.execute("ALTER TABLE price_observations ADD COLUMN catalog_id INTEGER")

        mapping = self._catalog_id_map()
        for table in ("price_observations", "items"):
            names = [row[0] for row in self.conn.execute(
                f"SELECT DISTINCT category FROM {table} WHERE catalog_id IS NULL")]
            unknown = [n for n in names if n not in mapping]
            if unknown:
                raise RuntimeError(
                    f"No catalog id known for {unknown!r} in {table}. Add them to "
                    f"HISTORICAL_CATALOG_IDS; guessing would silently merge one "
                    f"category's price history into another."
                )
            self.conn.executemany(
                f"UPDATE {table} SET catalog_id = ? "
                f"WHERE category = ? AND catalog_id IS NULL",
                [(mapping[n], n) for n in names],
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- items & observations ------------------------------------------------

    def upsert_item(
        self, item: VintedItem, *, brand: str, category: str, catalog_id: int,
        gender: str, garment_type: str,
    ) -> None:
        """Insert a new item or refresh an existing one's price/last_seen."""
        now = _utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO items (id, brand, brand_title, category, catalog_id, gender,
                               garment_type, title, price, currency, size, condition,
                               url, image_url, first_seen, last_seen, active,
                               favourite_count, view_count, listed_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                price           = excluded.price,
                size            = excluded.size,
                condition       = excluded.condition,
                image_url       = excluded.image_url,
                last_seen       = excluded.last_seen,
                active          = 1,
                favourite_count = excluded.favourite_count,
                view_count      = excluded.view_count,
                listed_ts       = COALESCE(items.listed_ts, excluded.listed_ts)
            """,
            (
                item.id, brand, item.brand_title, category, catalog_id, gender,
                garment_type, item.title, item.price, item.currency or "GBP", item.size,
                item.condition, item.url, item.image_url, now, now,
                item.favourite_count, item.view_count, item.listed_ts,
            ),
        )

    def add_observation(
        self, item: VintedItem, *, brand: str, category: str, catalog_id: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO price_observations
                   (item_id, brand, category, catalog_id, price, observed)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item.id, brand, category, catalog_id, item.price, _utc_now_iso()),
        )

    def mark_stale_items(self, stale_days: int) -> int:
        """Mark items unseen for `stale_days` as inactive. Returns count updated."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).replace(
            microsecond=0
        ).isoformat()
        cur = self.conn.execute(
            "UPDATE items SET active = 0 WHERE active = 1 AND last_seen < ?", (cutoff,)
        )
        return cur.rowcount

    def prune_observations(self, window_days: int) -> int:
        """Drop observations older than the baseline window to bound repo size."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).replace(
            microsecond=0
        ).isoformat()
        cur = self.conn.execute(
            "DELETE FROM price_observations WHERE observed < ?", (cutoff,)
        )
        return cur.rowcount

    def prune_items(self, keep_days: int) -> int:
        """Delete listings that are gone and were never hot. Returns rows deleted.

        This is the only thing bounding the file's size. `mark_stale_items` merely
        flips `active`, observations are pruned on their own much longer window,
        and the file is committed to git -- which hard-fails above 100 MB.

        Dropping these rows costs nothing analytically:

        - baselines are computed from `price_observations` alone, which carries
          its own brand/price and never joins `items` (see `observations_within`),
          so every baseline survives untouched
        - deal detection only ever reads `active_items()`

        The two exclusions are both load-bearing. Alerted items are the site's
        sold / time-to-sell history (`alerted JOIN items` in the site generator)
        and a good many of them are already inactive, so pruning them would erase
        what the page shows. `keep_days` is the grace period for a relisted item
        to come back before we forget it.

        **Call this after `replace_deals`, not before.** `deals.item_id`
        REFERENCES `items(id)` and foreign keys are on, so while `deals` still
        holds the previous run's rows a delete here can hit an item that run had
        flagged and this one has just marked inactive.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).replace(
            microsecond=0
        ).isoformat()
        cur = self.conn.execute(
            """
            DELETE FROM items
            WHERE active = 0
              AND last_seen < ?
              AND id NOT IN (SELECT item_id FROM alerted)
            """,
            (cutoff,),
        )
        return cur.rowcount

    def vacuum(self) -> int:
        """Repack the file, returning the bytes reclaimed.

        Worth doing only after a prune. Deleting rows leaves the pages allocated
        and partially filled -- `freelist_count` stays near zero and the file
        never shrinks -- so without this the prune frees nothing on disk. VACUUM
        cannot run inside a transaction, hence the commit first.
        """
        self.conn.commit()
        before = self.path.stat().st_size
        self.conn.execute("VACUUM")
        return before - self.path.stat().st_size

    # -- baselines -----------------------------------------------------------

    def observations_within(self, window_days: int) -> list[Observation]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).replace(
            microsecond=0
        ).isoformat()
        rows = self.conn.execute(
            "SELECT brand, category, price FROM price_observations WHERE observed >= ?",
            (cutoff,),
        ).fetchall()
        return [Observation(r["brand"], r["category"], r["price"]) for r in rows]

    def replace_baselines(self, baselines: list[tuple[str, str, float, float, int]]) -> None:
        now = _utc_now_iso()
        self.conn.execute("DELETE FROM baselines")
        self.conn.executemany(
            """
            INSERT INTO baselines (brand, category, median, mad, sample_size, computed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [(b, c, med, mad, n, now) for (b, c, med, mad, n) in baselines],
        )

    # -- deals ---------------------------------------------------------------

    def active_items(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM items WHERE active = 1").fetchall()

    def replace_deals(self, deals: list[dict]) -> None:
        now = _utc_now_iso()
        self.conn.execute("DELETE FROM deals")
        self.conn.executemany(
            """
            INSERT INTO deals (item_id, brand, category, price, baseline, baseline_src,
                               discount_pct, deal_score, flagged_at)
            VALUES (:item_id, :brand, :category, :price, :baseline, :baseline_src,
                    :discount_pct, :deal_score, :flagged_at)
            """,
            [{**d, "flagged_at": now} for d in deals],
        )

    def deals_for_site(self) -> list[sqlite3.Row]:
        """Deals joined with their item details, best discount first."""
        return self.conn.execute(
            """
            SELECT d.*, i.title, i.brand_title, i.gender, i.garment_type, i.size,
                   i.condition, i.url, i.image_url, i.first_seen, i.last_seen
            FROM deals d
            JOIN items i ON i.id = d.item_id
            WHERE i.active = 1
            ORDER BY d.discount_pct DESC
            """
        ).fetchall()

    def merge_alerts(self, hot_db_path: str | Path) -> int:
        """Copy the poller's alert log — and the listings themselves — into the DB.

        Two things are folded in, and the second is easy to overlook:

        1. The alert log, so dedup survives the Actions cache being evicted.
        2. The listing metadata, so every alerted item has a title, photo and URL
           to render. This matters because `items` is otherwise only populated by
           the *daily* scrape: a listing that appeared at 14:00 and sold by 14:20
           would be alerted on and then be unrenderable, and those are precisely
           the listings this system exists to catch.

        Scraped rows always win over poller rows where both exist — the daily
        scrape carries `catalog_id` and price history the poller never sees.
        """
        hot_path = Path(hot_db_path)
        if not hot_path.exists():
            return 0
        self.conn.execute("ATTACH DATABASE ? AS hot", (f"file:{hot_path}?mode=ro",))
        try:
            # Listings first, so the alerted rows below always have something to
            # join against.
            self.conn.execute(
                """
                INSERT INTO items (id, brand, brand_title, category, gender,
                                   garment_type, title, price, currency, size,
                                   condition, url, image_url, first_seen, last_seen,
                                   active, listed_ts)
                SELECT m.item_id, m.brand, m.brand_title, m.category, m.gender,
                       m.garment_type, m.title, m.price, m.currency, m.size,
                       m.condition, m.url, m.image_url,
                       strftime('%Y-%m-%dT%H:%M:%S+00:00', m.first_seen, 'unixepoch'),
                       strftime('%Y-%m-%dT%H:%M:%S+00:00', m.last_seen, 'unixepoch'),
                       CASE WHEN m.gone_at IS NULL THEN 1 ELSE 0 END,
                       m.listed_ts
                FROM hot.item_meta m
                JOIN hot.alerts a ON a.item_id = m.item_id
                WHERE true
                ON CONFLICT(id) DO NOTHING
                """
            )
            cur = self.conn.execute(
                """
                INSERT INTO alerted (item_id, alerted_at, heat, fav_rate, view_rate,
                                     price, baseline, sold_at, seconds_to_sell)
                SELECT a.item_id, a.alerted_at, a.heat, a.fav_rate, a.view_rate,
                       a.price, a.baseline,
                       strftime('%Y-%m-%dT%H:%M:%S+00:00', m.gone_at, 'unixepoch'),
                       CAST(m.gone_at - m.listed_ts AS INTEGER)
                FROM hot.alerts a
                LEFT JOIN hot.item_meta m ON m.item_id = a.item_id
                WHERE true
                ON CONFLICT(item_id) DO UPDATE SET
                    sold_at         = COALESCE(alerted.sold_at, excluded.sold_at),
                    seconds_to_sell = COALESCE(alerted.seconds_to_sell,
                                               excluded.seconds_to_sell)
                """
            )
            merged = cur.rowcount
            # The attached database is enrolled in the open transaction, and
            # SQLite refuses to DETACH inside one. Commit before detaching.
            self.conn.commit()
            return merged
        finally:
            self.conn.execute("DETACH DATABASE hot")

    def alerted_ids(self) -> list[int]:
        return [row[0] for row in self.conn.execute("SELECT item_id FROM alerted")]

    def commit(self) -> None:
        self.conn.commit()

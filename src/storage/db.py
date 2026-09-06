"""SQLite persistence: schema init, upserts, observations, baselines, deals."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..vinted.models import VintedItem

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "vinted.db"


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

    def add_observation(self, item: VintedItem, *, brand: str, category: str) -> None:
        self.conn.execute(
            """
            INSERT INTO price_observations (item_id, brand, category, price, observed)
            VALUES (?, ?, ?, ?, ?)
            """,
            (item.id, brand, category, item.price, _utc_now_iso()),
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

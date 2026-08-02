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
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text())
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- items & observations ------------------------------------------------

    def upsert_item(self, item: VintedItem, *, brand: str, category: str, catalog_id: int) -> None:
        """Insert a new item or refresh an existing one's price/last_seen."""
        now = _utc_now_iso()
        self.conn.execute(
            """
            INSERT INTO items (id, brand, brand_title, category, catalog_id, title,
                               price, currency, size, url, image_url,
                               first_seen, last_seen, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(id) DO UPDATE SET
                price     = excluded.price,
                size      = excluded.size,
                image_url = excluded.image_url,
                last_seen = excluded.last_seen,
                active    = 1
            """,
            (
                item.id, brand, item.brand_title, category, catalog_id, item.title,
                item.price, item.currency or "GBP", item.size, item.url, item.image_url,
                now, now,
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
            SELECT d.*, i.title, i.brand_title, i.size, i.url, i.image_url,
                   i.first_seen, i.last_seen
            FROM deals d
            JOIN items i ON i.id = d.item_id
            WHERE i.active = 1
            ORDER BY d.discount_pct DESC
            """
        ).fetchall()

    def commit(self) -> None:
        self.conn.commit()

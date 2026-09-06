"""Hot-path state: a small, disposable SQLite file the poller works against.

Deliberately NOT `data/vinted.db`. That file is 72 MB, committed to git, and
rebuilt once a day; writing to it every 60 seconds would be impossible to commit
and would bloat the repo without bound. This one holds only the last few hours of
observations, never enters git, and can be thrown away — losing it costs a few
duplicate alerts, nothing more.

It lives in the GitHub Actions cache between runs. Everything here assumes it may
vanish at any moment and be recreated empty.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..vinted.models import VintedItem
from .hotness import Sample

log = logging.getLogger(__name__)

DEFAULT_HOT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "hot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS item_meta (
    item_id      INTEGER PRIMARY KEY,
    brand        TEXT,
    category     TEXT,
    gender       TEXT,
    garment_type TEXT,
    title        TEXT,
    price        REAL,
    currency     TEXT,
    size         TEXT,
    condition    TEXT,
    url          TEXT,
    image_url    TEXT,
    promoted     INTEGER NOT NULL DEFAULT 0,
    listed_ts    INTEGER,
    first_seen   REAL NOT NULL,      -- unix seconds, when WE first saw it
    last_seen    REAL NOT NULL
);

-- Attention over time. The whole hotness signal is the shape of this table.
CREATE TABLE IF NOT EXISTS item_samples (
    item_id    INTEGER NOT NULL,
    observed   REAL NOT NULL,        -- unix seconds
    favourites INTEGER,
    views      INTEGER,
    PRIMARY KEY (item_id, observed)
);
CREATE INDEX IF NOT EXISTS idx_samples_observed ON item_samples(observed);

-- Alert dedup. Merged into the committed DB daily so it survives cache loss.
CREATE TABLE IF NOT EXISTS alerts (
    item_id    INTEGER PRIMARY KEY,
    alerted_at TEXT NOT NULL,
    heat       REAL,
    fav_rate   REAL,
    view_rate  REAL,
    price      REAL,
    baseline   REAL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class HotState:
    def __init__(self, path: str | Path = DEFAULT_HOT_DB):
        self.path = Path(path)
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        # The poller writes constantly and we can afford to lose the last few
        # writes on a hard kill, but we cannot afford a corrupt file on the
        # 6-hour Actions timeout. WAL + NORMAL is the right trade here.
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "HotState":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- recording ----------------------------------------------------------

    def record(
        self,
        item: VintedItem,
        *,
        brand: str,
        category: str,
        gender: str,
        garment_type: str,
        now: float,
    ) -> None:
        """Upsert a listing's metadata and append one attention sample.

        `first_seen` is preserved across upserts — it is the fallback age signal
        when Vinted gives us no photo timestamp.
        """
        self.conn.execute(
            """
            INSERT INTO item_meta (item_id, brand, category, gender, garment_type,
                                   title, price, currency, size, condition, url,
                                   image_url, promoted, listed_ts, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                price     = excluded.price,
                size      = excluded.size,
                condition = excluded.condition,
                image_url = excluded.image_url,
                promoted  = excluded.promoted,
                last_seen = excluded.last_seen
            """,
            (
                item.id, brand, category, gender, garment_type, item.title,
                item.price, item.currency or "GBP", item.size, item.condition,
                item.url, item.image_url, int(item.promoted), item.listed_ts, now, now,
            ),
        )
        # A repeated poll within the same second is a no-op rather than an error.
        self.conn.execute(
            """
            INSERT INTO item_samples (item_id, observed, favourites, views)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(item_id, observed) DO NOTHING
            """,
            (item.id, now, item.favourite_count, item.view_count),
        )

    def commit(self) -> None:
        self.conn.commit()

    # -- reading ------------------------------------------------------------

    def samples_by_item(self, since: float) -> dict[int, list[Sample]]:
        """All samples newer than `since`, grouped by item, ascending in time.

        One query rather than one per item: the poller does this every cycle and
        a few hundred round trips per minute would dominate its runtime.
        """
        rows = self.conn.execute(
            """
            SELECT item_id, observed, favourites, views FROM item_samples
            WHERE observed >= ? ORDER BY item_id, observed
            """,
            (since,),
        ).fetchall()
        grouped: dict[int, list[Sample]] = {}
        for row in rows:
            grouped.setdefault(row["item_id"], []).append(
                Sample(row["observed"], row["favourites"], row["views"])
            )
        return grouped

    def meta_for(self, item_ids: list[int]) -> dict[int, sqlite3.Row]:
        if not item_ids:
            return {}
        placeholders = ",".join("?" * len(item_ids))
        rows = self.conn.execute(
            f"SELECT * FROM item_meta WHERE item_id IN ({placeholders})", item_ids
        ).fetchall()
        return {row["item_id"]: row for row in rows}

    def age_seconds(self, meta: sqlite3.Row, now: float) -> float:
        """Best available age for a listing.

        Prefer Vinted's photo timestamp (when the listing actually went up); fall
        back to when we first saw it. The fallback under-states age — we may have
        found it an hour late — which makes the item look *hotter* than it is, so
        callers gate on `min_age_minutes` to avoid acting on a thin estimate.
        """
        if meta["listed_ts"]:
            return max(now - float(meta["listed_ts"]), 0.0)
        return max(now - float(meta["first_seen"]), 0.0)

    # -- alert dedup --------------------------------------------------------

    def has_alerted(self, item_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM alerts WHERE item_id = ?", (item_id,)
        ).fetchone()
        return row is not None

    def mark_alerted(
        self, item_id: int, *, heat: float, fav_rate: float | None,
        view_rate: float | None, price: float, baseline: float | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO alerts (item_id, alerted_at, heat, fav_rate, view_rate, price, baseline)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO NOTHING
            """,
            (item_id, _utc_now_iso(), heat, fav_rate, view_rate, price, baseline),
        )

    def alerts_since(self, since_iso: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM alerts WHERE alerted_at >= ? ORDER BY alerted_at", (since_iso,)
        ).fetchall()

    def seed_alerted(self, item_ids: list[int]) -> int:
        """Pre-fill dedup from the committed DB after a cache miss.

        Without this, a lost cache would re-alert on everything still live that we
        already pushed — the fastest way to make someone mute the notifications.
        """
        if not item_ids:
            return 0
        now = _utc_now_iso()
        cur = self.conn.executemany(
            "INSERT INTO alerts (item_id, alerted_at) VALUES (?, ?) "
            "ON CONFLICT(item_id) DO NOTHING",
            [(i, now) for i in item_ids],
        )
        self.conn.commit()
        return cur.rowcount

    # -- housekeeping -------------------------------------------------------

    def prune(self, *, sample_hours: float = 6.0, alert_hours: float = 72.0,
              now: float | None = None) -> tuple[int, int]:
        """Drop state we no longer need, so the cached file stays small.

        Samples age out fast (velocity only looks back ~30 minutes). Alerts are
        kept much longer — they are the dedup record, and a listing we alerted on
        yesterday must not fire again today just because it is still listed.
        """
        import time as _time
        now = now if now is not None else _time.time()
        samples = self.conn.execute(
            "DELETE FROM item_samples WHERE observed < ?", (now - sample_hours * 3600,)
        ).rowcount
        self.conn.execute(
            "DELETE FROM item_meta WHERE last_seen < ?", (now - sample_hours * 3600,)
        )
        cutoff_iso = datetime.fromtimestamp(
            now - alert_hours * 3600, tz=timezone.utc
        ).replace(microsecond=0).isoformat()
        alerts = self.conn.execute(
            "DELETE FROM alerts WHERE alerted_at < ?", (cutoff_iso,)
        ).rowcount
        self.conn.commit()
        return samples, alerts

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

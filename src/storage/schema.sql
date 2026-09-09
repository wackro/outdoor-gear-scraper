-- SQLite schema for the Vinted bargain scraper.
-- This database is committed to the repo and is the sole source of truth.

-- Every distinct listing we've seen, deduplicated by Vinted's own item id.
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY,        -- Vinted item id (natural dedup key)
    brand        TEXT NOT NULL,              -- normalized brand key from config
    brand_title  TEXT,                       -- raw brand_title from the API
    catalog_id   INTEGER,                    -- Vinted catalog id; the category key
    gender       TEXT,                       -- 'men' | 'women'
    garment_type TEXT,                       -- 'clothes' | 'trousers' | 'shoes'
    title        TEXT,
    price        REAL NOT NULL,              -- in the configured currency
    currency     TEXT NOT NULL DEFAULT 'GBP',
    size         TEXT,
    condition    TEXT,                       -- Vinted status, e.g. "Very good"
    url          TEXT,
    image_url    TEXT,
    first_seen   TEXT NOT NULL,              -- ISO8601 UTC
    last_seen    TEXT NOT NULL,              -- refreshed each run the item still appears
    active       INTEGER NOT NULL DEFAULT 1  -- 0 once it disappears (likely sold)
);

-- Append-only price observations that feed the baseline. Prunable by window_days.
--
-- `catalog_id` is the category, and is per-observation rather than looked up from
-- `items`: a listing can be returned by more than one category sweep, and this
-- records which one actually saw it at this price.
CREATE TABLE IF NOT EXISTS price_observations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    INTEGER NOT NULL,
    brand      TEXT NOT NULL,
    catalog_id INTEGER,
    price      REAL NOT NULL,
    observed   TEXT NOT NULL                 -- ISO8601 UTC (run timestamp)
);

-- Materialized baseline per brand+category, recomputed each run.
CREATE TABLE IF NOT EXISTS baselines (
    brand        TEXT NOT NULL,
    catalog_id   INTEGER NOT NULL,
    median       REAL,
    mad          REAL,                       -- median absolute deviation (robust spread)
    sample_size  INTEGER NOT NULL,
    computed_at  TEXT NOT NULL,
    PRIMARY KEY (brand, catalog_id)
);

-- Deals flagged for the current site. Fully rebuilt each run.
CREATE TABLE IF NOT EXISTS deals (
    item_id      INTEGER PRIMARY KEY REFERENCES items(id),
    brand        TEXT NOT NULL,
    catalog_id   INTEGER NOT NULL,
    price        REAL NOT NULL,
    baseline     REAL NOT NULL,              -- reference price used
    baseline_src TEXT NOT NULL,              -- 'history' | 'rrp'
    discount_pct REAL NOT NULL,              -- 1 - price/baseline
    deal_score   REAL NOT NULL,              -- robust z-score
    flagged_at   TEXT NOT NULL
);

-- Listings we have already pushed an alert for. Durable dedup: the poller's own
-- state lives in a throwaway cache file, so without this a lost cache would
-- re-alert on everything still listed.
CREATE TABLE IF NOT EXISTS alerted (
    item_id    INTEGER PRIMARY KEY,
    alerted_at TEXT NOT NULL,
    heat       REAL,
    fav_rate   REAL,                       -- favourites/hour at alert time
    view_rate  REAL,
    price      REAL,
    baseline   REAL
);

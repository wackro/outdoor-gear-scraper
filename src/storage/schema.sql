-- SQLite schema for the Vinted bargain scraper.
-- This database is committed to the repo and is the sole source of truth.

-- Every distinct listing we've seen, deduplicated by Vinted's own item id.
CREATE TABLE IF NOT EXISTS items (
    id           INTEGER PRIMARY KEY,        -- Vinted item id (natural dedup key)
    brand        TEXT NOT NULL,              -- normalized brand key from config
    brand_title  TEXT,                       -- raw brand_title from the API
    category     TEXT NOT NULL,              -- normalized category key from config
    catalog_id   INTEGER,
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
CREATE INDEX IF NOT EXISTS idx_items_brand_cat ON items(brand, category);
CREATE INDEX IF NOT EXISTS idx_items_active ON items(active, last_seen);

-- Append-only price observations that feed the baseline. Prunable by window_days.
CREATE TABLE IF NOT EXISTS price_observations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id   INTEGER NOT NULL,
    brand     TEXT NOT NULL,
    category  TEXT NOT NULL,
    price     REAL NOT NULL,
    observed  TEXT NOT NULL                  -- ISO8601 UTC (run timestamp)
);
CREATE INDEX IF NOT EXISTS idx_obs_bracket ON price_observations(brand, category, observed);

-- Materialized baseline per brand+category, recomputed each run.
CREATE TABLE IF NOT EXISTS baselines (
    brand        TEXT NOT NULL,
    category     TEXT NOT NULL,
    median       REAL,
    mad          REAL,                       -- median absolute deviation (robust spread)
    sample_size  INTEGER NOT NULL,
    computed_at  TEXT NOT NULL,
    PRIMARY KEY (brand, category)
);

-- Deals flagged for the current site. Fully rebuilt each run.
CREATE TABLE IF NOT EXISTS deals (
    item_id      INTEGER PRIMARY KEY REFERENCES items(id),
    brand        TEXT NOT NULL,
    category     TEXT NOT NULL,
    price        REAL NOT NULL,
    baseline     REAL NOT NULL,              -- reference price used
    baseline_src TEXT NOT NULL,              -- 'history' | 'rrp'
    discount_pct REAL NOT NULL,              -- 1 - price/baseline
    deal_score   REAL NOT NULL,              -- robust z-score
    flagged_at   TEXT NOT NULL
);

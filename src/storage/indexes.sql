-- Indexes, applied *after* the migrations in db.py rather than alongside the
-- tables in schema.sql.
--
-- They name columns the migrations add, so creating them with the tables fails
-- on any database old enough to still need migrating: CREATE INDEX ... ON
-- items(brand, catalog_id) cannot run before catalog_id exists. Ordering is the
-- whole reason this is a separate file.

CREATE INDEX IF NOT EXISTS idx_items_brand_cat ON items(brand, catalog_id);
CREATE INDEX IF NOT EXISTS idx_items_active ON items(active, last_seen);
CREATE INDEX IF NOT EXISTS idx_obs_bracket ON price_observations(brand, catalog_id, observed);
CREATE INDEX IF NOT EXISTS idx_alerted_at ON alerted(alerted_at);

"""Rendering the shell and its fallback feed."""
import json
import re
import time

import pytest

from src.site.generator import build_fallback_feed, render_site, resolve_feed_url
from src.storage.db import Database
from src.vinted.models import VintedItem


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / "v.db") as database:
        yield database


def add_alerted(db, item_id=1, *, sold=False):
    db.upsert_item(
        VintedItem(id=item_id, title="Beta AR", price=38.0, currency="GBP",
                   brand_title="Rab", size="M", condition="Very good",
                   url=f"https://vinted.co.uk/items/{item_id}", image_url="img",
                   favourite_count=20, view_count=300,
                   listed_ts=int(time.time() - 1800)),
        brand="rab", category="men_jackets", catalog_id=2052,
        gender="men", garment_type="clothes",
    )
    db.conn.execute(
        "INSERT INTO alerted (item_id, alerted_at, heat, fav_rate, view_rate, "
        "price, baseline, sold_at, seconds_to_sell) VALUES (?,?,?,?,?,?,?,?,?)",
        (item_id, "2026-09-06T10:00:00+00:00", 0.9, 40.0, 600.0, 38.0, 150.0,
         "2026-09-06T10:04:00+00:00" if sold else None, 240 if sold else None),
    )
    db.commit()


class TestFeedUrl:
    def test_explicit_config_wins(self):
        assert resolve_feed_url("https://example.com/f.json", "hot-feed") \
            == "https://example.com/f.json"

    def test_derived_from_github_repository(self, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", "wackro/outdoor-gear-scraper")
        assert resolve_feed_url("", "hot-feed") == (
            "https://raw.githubusercontent.com/wackro/outdoor-gear-scraper/"
            "hot-feed/hot.json"
        )

    def test_falls_back_to_a_relative_path(self, monkeypatch):
        # So the page still works opened straight off disk.
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        assert resolve_feed_url("", "hot-feed") == "hot.json"


class TestFallbackFeed:
    def test_shape_matches_the_live_feed(self, db):
        add_alerted(db)
        feed = build_fallback_feed(db)
        assert feed["stale_fallback"] is True
        entry = feed["items"][0]
        for key in ("id", "title", "brand_title", "section", "price", "heat",
                    "alerted", "sold", "url", "image_url"):
            assert key in entry
        assert entry["alerted"] is True

    def test_sold_listings_carry_their_time(self, db):
        add_alerted(db, sold=True)
        feed = build_fallback_feed(db)
        assert feed["items"][0]["sold"] is True
        assert feed["median_seconds_to_sell"] == 240

    def test_empty_database(self, db):
        feed = build_fallback_feed(db)
        assert feed["items"] == []
        assert feed["counts"]["tracked"] == 0


class TestRender:
    def test_writes_a_small_shell_with_the_feed_inlined(self, db, tmp_path):
        add_alerted(db)
        out = tmp_path / "docs"
        path = render_site(db, output_dir=out, feed_url="https://example.com/f.json")
        html = path.read_text()

        # No server-rendered cards: everything comes from the feed.
        assert 'id="feed"' in html
        assert 'class="card"' not in html
        assert path.stat().st_size < 100_000

        assert 'data-feed-url="https://example.com/f.json"' in html
        assert (out / "hot.json").exists()
        assert (out / "static" / "hot.js").exists()
        assert (out / ".nojekyll").exists()

        bootstrap = re.search(
            r'id="feed-bootstrap">(.*?)</script>', html, re.S).group(1)
        assert json.loads(bootstrap.replace("\\u003c", "<"))["items"][0]["id"] == 1

    def test_a_title_containing_a_script_tag_cannot_break_out(self, db, tmp_path):
        db.upsert_item(
            VintedItem(id=2, title="</script><script>alert(1)</script>",
                       price=10.0, currency="GBP", brand_title="Rab", size="M",
                       condition="Good", url="u", image_url="i"),
            brand="rab", category="men_jackets", catalog_id=1,
            gender="men", garment_type="clothes",
        )
        db.conn.execute(
            "INSERT INTO alerted (item_id, alerted_at, price) VALUES (2, 'x', 1.0)")
        db.commit()
        html = render_site(db, output_dir=tmp_path / "docs").read_text()
        bootstrap = re.search(
            r'id="feed-bootstrap">(.*?)</script>', html, re.S).group(1)
        # The closing tag must be escaped inside the JSON block.
        assert "</script>" not in bootstrap
        assert json.loads(bootstrap.replace("\\u003c", "<"))["items"][0]["title"] \
            == "</script><script>alert(1)</script>"


def test_javascript_urls_are_rejected_by_the_client():
    """The client whitelists URL schemes before assigning href/src.

    Card text is always set via textContent, but a URL is executable if it
    carries a javascript: scheme, and feed content describes listings we do not
    control. Asserted against the shipped script so the guard can't be dropped.
    """
    from pathlib import Path
    source = Path("src/site/static/hot.js").read_text()
    assert "function safeUrl" in source
    assert "/^https?:\\/\\//i.test(value)" in source
    assert "link.href = item.url" not in source
    assert "img.src = item.image_url" not in source


class TestRenderOnly:
    """Rebuilding the page without scraping.

    The page shell is code; tying its rebuild to a Vinted scrape meant a
    template change could only reach the site once a day, on the back of a
    network round trip it didn't need.
    """

    def test_renders_without_touching_the_network(self, tmp_path, monkeypatch):
        import src.run as run

        def explode(*a, **k):
            raise AssertionError("render-only must not construct a client")

        monkeypatch.setattr(run, "VintedClient", explode)
        monkeypatch.setattr(run, "Database", lambda *a, **k: _db(tmp_path))
        rendered = {}
        monkeypatch.setattr(run, "render_site",
                            lambda db, **kw: rendered.update(kw) or tmp_path)

        assert run.main(["--render-only"]) == 0
        assert "feed_url" in rendered and "feed_branch" in rendered

    def test_full_run_is_still_the_default(self, monkeypatch):
        import src.run as run
        called = []
        monkeypatch.setattr(run, "render_only", lambda cfg: called.append("render") or 0)
        run.main(["--render-only"])
        assert called == ["render"]


def _db(tmp_path):
    from src.storage.db import Database
    return Database(tmp_path / "v.db")


def test_fallback_feed_survives_a_database_without_alert_tables(tmp_path):
    """The committed DB predates the alert tables until the daily job reruns.

    Rendering the site must not be what fails in that window.
    """
    import sqlite3

    from src.site.generator import build_fallback_feed
    from src.storage.db import Database

    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE items (id INTEGER PRIMARY KEY)")   # no `alerted`
    raw.commit()
    raw.close()

    class Stub:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    feed = build_fallback_feed(Stub())
    assert feed["items"] == []
    assert feed["counts"]["tracked"] == 0


def test_shipped_config_pins_an_absolute_feed_url():
    """The URL must not depend on where the page happened to be rendered.

    Deriving it from $GITHUB_REPOSITORY meant a render outside Actions produced
    the relative "hot.json", which resolves to the empty build-time fallback
    beside the page instead of the live feed -- a page that looks healthy and is
    silently, permanently empty.
    """
    from src.config import load_config
    from src.site.generator import resolve_feed_url

    config = load_config("config/config.yaml")
    url = resolve_feed_url(config.site.feed_url, config.poll.feed_branch)
    assert url.startswith("https://")
    assert config.poll.feed_branch in url


def test_a_pinned_url_beats_the_environment(monkeypatch):
    from src.site.generator import resolve_feed_url
    monkeypatch.setenv("GITHUB_REPOSITORY", "someone/else")
    assert resolve_feed_url("https://pinned.example/f.json", "hot-feed") \
        == "https://pinned.example/f.json"


class TestCategoryConfig:
    """Categories are identified by id, never by title.

    Vinted's men's tree contains genuine duplicate titles -- "Outerwear" is both
    1206 and 581, "Shorts" is both 80 and 586 -- so a title lookup could bind to
    the wrong node with no error and no way to notice.
    """

    def _write(self, tmp_path, categories: str):
        base = open("config/config.yaml").read()
        start = base.index("categories:")
        end = base.index("\n\n", start)
        path = tmp_path / "c.yaml"
        path.write_text(base[:start] + "categories:\n" + categories + base[end:])
        return path

    def test_shipped_config_is_all_ids_with_unique_names(self):
        from src.config import load_config
        cats = load_config("config/config.yaml").categories
        assert all(c.id for c in cats)
        assert len({c.id for c in cats}) == len(cats)
        assert len({c.name for c in cats}) == len(cats)

    def test_database_keys_are_preserved(self):
        """Renaming one orphans that category's accumulated price history."""
        from src.config import load_config
        names = {c.name for c in load_config("config/config.yaml").categories}
        for key in ("men_jackets", "men_jumpers_&_sweaters", "men_trousers",
                    "men_shoes", "men_bags_&_backpacks"):
            assert key in names, f"{key} would orphan its baselines"

    def test_a_missing_id_is_rejected(self, tmp_path):
        import pytest
        from src.config import load_config
        path = self._write(tmp_path, "  - {gender: men, type: clothes, name: men_x}\n")
        with pytest.raises(ValueError, match="needs an `id`"):
            load_config(path)

    def test_a_missing_name_is_rejected(self, tmp_path):
        import pytest
        from src.config import load_config
        path = self._write(tmp_path, "  - {id: 1, gender: men, type: clothes}\n")
        with pytest.raises(ValueError, match="needs a `name`"):
            load_config(path)

    def test_a_duplicate_id_is_rejected(self, tmp_path):
        # Silent otherwise: it just wastes a rotation slot re-reading one category.
        import pytest
        from src.config import load_config
        path = self._write(tmp_path,
            "  - {id: 1, gender: men, type: clothes, name: men_a}\n"
            "  - {id: 1, gender: men, type: clothes, name: men_b}\n")
        with pytest.raises(ValueError, match="Duplicate category id 1"):
            load_config(path)

    def test_a_duplicate_name_is_rejected(self, tmp_path):
        # Silent otherwise: two categories' price history merges into one bracket.
        import pytest
        from src.config import load_config
        path = self._write(tmp_path,
            "  - {id: 1, gender: men, type: clothes, name: men_a}\n"
            "  - {id: 2, gender: men, type: clothes, name: men_a}\n")
        with pytest.raises(ValueError, match="Duplicate category name"):
            load_config(path)

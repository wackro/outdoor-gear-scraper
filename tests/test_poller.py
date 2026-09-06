"""The poll loop itself, driven against a stubbed Vinted client.

This is the only test that exercises sweep -> record -> score -> notify as one
piece, which is where the wiring bugs live.
"""
import time

import pytest

from src.config import load_config
from src.hot.poller import Poller
from src.hot.state import HotState
from src.notify.base import Notifier
from src.vinted.brand_resolver import BrandResolution
from src.vinted.client import VintedBlocked, VintedError
from src.vinted.models import VintedItem

NOW = time.time()


class FakeClient:
    """Returns a scripted page per call, and counts requests."""

    def __init__(self, pages, error=None):
        self.pages = pages
        self.error = error
        self.calls = 0
        self.sessions_reset = 0

    def fetch_page(self, brand_ids, catalog_id, page=1):
        self.calls += 1
        if self.error:
            raise self.error
        return self.pages.pop(0) if self.pages else []

    def throttle(self):
        pass

    def reset_session(self):
        self.sessions_reset += 1


class Recorder(Notifier):
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)
        return True


def make_item(item_id, favourites, *, listed_minutes_ago=30.0):
    return VintedItem(
        id=item_id, title="Beta AR", price=30.0, currency="GBP", brand_title="Rab",
        size="M", condition="Very good", url=f"u/{item_id}", image_url="i",
        favourite_count=favourites, view_count=favourites * 10,
        listed_ts=int(NOW - listed_minutes_ago * 60),
    )


@pytest.fixture
def build(tmp_path):
    """Build a Poller wired to a stubbed client.

    `categories` defaults to 1 so that one `run(once=True)` is exactly one
    request, and each scripted page therefore lands in its own cycle. That
    matters because a sweep stamps every item it sees with a single timestamp —
    two pages inside one cycle would collapse into one observation and there
    would be no delta to measure.
    """
    def _build(pages, error=None, categories=1):
        config = load_config("config/config.example.yaml")
        client = FakeClient(pages, error=error)
        notifier = Recorder()
        poller = Poller(
            config=config,
            state=HotState(tmp_path / "hot.db"),
            client=client,
            notifier=notifier,
            brands=BrandResolution(ids={"rab": 1}, name_by_title={"rab": "rab"}),
            category_ids={c.name: (c.id or 1) for c in config.categories},
        )
        poller.categories = poller.categories[:categories]
        return poller, client, notifier
    return _build


def test_a_listing_gaining_likes_produces_one_alert(build):
    # Same item twice: cold, then hot. That delta is the whole signal.
    poller, _, notifier = build([[make_item(1, 0)], [make_item(1, 25)]])
    poller.run(once=True)
    time.sleep(1.1)          # distinct observation timestamps
    poller.run(once=True)

    assert len(notifier.sent) == 1
    alert = notifier.sent[0]
    assert alert.item_id == 1
    assert alert.favourites == 25
    assert alert.measured


def test_the_same_listing_never_alerts_twice(build):
    poller, _, notifier = build([
        [make_item(1, 0)], [make_item(1, 25)], [make_item(1, 60)],
    ])
    for _ in range(3):
        poller.run(once=True)
        time.sleep(1.1)
    assert len(notifier.sent) == 1


def test_a_cold_listing_is_tracked_but_not_alerted(build):
    poller, _, notifier = build([[make_item(1, 0)], [make_item(1, 1)]])
    poller.run(once=True)
    time.sleep(1.1)
    poller.run(once=True)
    assert notifier.sent == []
    assert poller.stats.items_tracked == 2


def test_categories_are_swept_in_rotation(build):
    # Two per cycle out of fourteen, so a full pass takes seven cycles rather
    # than firing fourteen requests a minute at a DataDome-protected endpoint.
    poller, client, _ = build([], categories=14)
    poller.run(once=True)
    assert client.calls == poller.config.poll.deep_pages

    first = poller._next_categories()
    second = poller._next_categories()
    assert {c.name for c in first}.isdisjoint({c.name for c in second})


def test_rate_limiting_triggers_a_cooldown(build):
    poller, _, _ = build([], error=VintedBlocked("HTTP 429 (rate limited)"))
    poller.run(once=True)
    assert poller.stats.blocks == 1
    assert poller._cooldown >= poller.config.poll.cooldown_base_sec


def test_an_ordinary_error_is_not_treated_as_a_block(build):
    # Blocks are identified by exception type, not by pattern-matching the
    # message — which contains the query params, so a brand id like "403..."
    # would otherwise look like an HTTP 403 and trigger a needless cooldown.
    poller, _, _ = build([], error=VintedError("Request failed (brand_ids=403812)"))
    poller.run(once=True)
    assert poller.stats.blocks == 0
    assert poller._cooldown == 0.0


def test_a_failing_fetch_does_not_kill_the_run(build):
    poller, _, _ = build([], error=VintedError("boom"))
    assert poller.run(once=True) == 0
    assert poller.stats.errors == 1


def test_unwatched_brands_are_ignored(build):
    poller, _, notifier = build([[
        VintedItem(id=9, title="x", price=5.0, currency="GBP",
                   brand_title="Some Random Brand", size="M", condition="Good",
                   url="u", image_url="i", favourite_count=99),
    ]])
    poller.run(once=True)
    assert poller.stats.items_tracked == 0


def test_run_summary_reports_what_happened(build):
    poller, _, _ = build([[make_item(1, 0)]])
    poller.run(once=True)
    summary = poller.stats.as_markdown(poller.bar)
    assert "Vinted poller run" in summary
    assert "Alerts sent" in summary

"""Parsing catalog JSON — which drifts without warning, so it parses defensively."""
from src.vinted.models import VintedItem

BASE = "https://www.vinted.co.uk"


def parse(**overrides):
    raw = {
        "id": 42, "title": "Beta AR", "brand_title": "Rab", "size_title": "M",
        "status": "Very good", "price": {"amount": "38.0", "currency_code": "GBP"},
        "favourite_count": 12, "view_count": 340, "promoted": False,
        "photo": {"full_size_url": "http://img", "high_resolution": {"timestamp": 1757000000}},
    }
    raw.update(overrides)
    return VintedItem.from_json(raw, base_url=BASE)


class TestAttentionFields:
    def test_reads_counters_and_timestamp(self):
        item = parse()
        assert (item.favourite_count, item.view_count) == (12, 340)
        assert item.listed_ts == 1757000000
        assert item.promoted is False

    def test_zero_is_a_real_reading(self):
        assert parse(favourite_count=0).favourite_count == 0

    def test_missing_counter_is_unknown_not_zero(self):
        # The distinction matters: "nobody liked it" would suppress the alert,
        # "we don't know" must leave the item scoreable on its other signal.
        assert parse(favourite_count=None).favourite_count is None
        item = VintedItem.from_json({"id": 1, "price": "5"}, base_url=BASE)
        assert item.favourite_count is None and item.view_count is None

    def test_unparseable_counter_is_unknown(self):
        assert parse(view_count="lots").view_count is None
        assert parse(view_count=True).view_count is None    # bool is not a count

    def test_negative_counter_is_unknown(self):
        # Nonsense in, nothing out — better than feeding -3 into the rate maths.
        assert parse(favourite_count=-3).favourite_count is None

    def test_promoted_flag(self):
        assert parse(promoted=True).promoted is True


class TestListedTimestamp:
    def test_missing_photo(self):
        assert parse(photo=None).listed_ts is None

    def test_photo_without_high_resolution(self):
        assert parse(photo={"full_size_url": "http://img"}).listed_ts is None

    def test_zero_timestamp_is_rejected(self):
        assert parse(photo={"high_resolution": {"timestamp": 0}}).listed_ts is None

    def test_unparseable_timestamp(self):
        assert parse(photo={"high_resolution": {"timestamp": "soon"}}).listed_ts is None


class TestExistingBehaviourStillHolds:
    def test_price_and_url_fallback(self):
        item = parse()
        assert item.price == 38.0 and item.currency == "GBP"
        assert item.url == f"{BASE}/items/42"

    def test_unusable_rows_are_skipped(self):
        assert VintedItem.from_json({"title": "no id"}, base_url=BASE) is None
        assert VintedItem.from_json({"id": 1, "price": "0"}, base_url=BASE) is None

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


class TestCatalogueServiceShape:
    """The catalogue service sends brand, size and condition somewhere new.

    Measured against the live service on 15 Sep: `favourite_count`, `view_count`
    and `photo` at 100%, and `brand_title`, `size_title`, `status` at **0%** —
    all three moved into a human-facing `item_box`. Reading the old fields would
    not error, it would drop every listing: `scrape()` maps the brand back to a
    config entry and skips anything it cannot match.
    """

    def raw(self, **overrides):
        base = {
            "id": 1,
            "price": {"amount": "38.0", "currency_code": "GBP"},
            "title": "Beta AR jacket",
            "favourite_count": 4,
            "item_box": {"first_line": "Rab", "second_line": "M · Very good"},
        }
        base.update(overrides)
        return base

    def parse(self, **overrides):
        return VintedItem.from_json(self.raw(**overrides),
                                    base_url="https://www.vinted.co.uk")

    def test_brand_comes_from_the_item_box(self):
        assert self.parse().brand_title == "Rab"

    def test_size_and_condition_are_split_out_of_one_line(self):
        item = self.parse()
        assert item.size == "M"
        assert item.condition == "Very good"

    def test_the_parts_are_classified_by_content_not_position(self):
        """Order is not dependable, and position-based parsing would put the
        condition in the size field the moment they swap."""
        item = self.parse(item_box={"first_line": "Rab", "second_line": "Very good · M"})
        assert item.size == "M"
        assert item.condition == "Very good"

    def test_a_listing_with_only_a_condition(self):
        """Common: plenty of listings carry no size at all."""
        item = self.parse(item_box={"first_line": "Rab", "second_line": "Good"})
        assert item.condition == "Good"
        assert item.size == ""

    def test_a_listing_with_only_a_size(self):
        item = self.parse(item_box={"first_line": "Rab", "second_line": "XL"})
        assert item.size == "XL"
        assert item.condition == ""

    def test_multi_word_sizes_survive(self):
        item = self.parse(item_box={"first_line": "Rab", "second_line": "UK 9.5 · Good"})
        assert item.size == "UK 9.5"
        assert item.condition == "Good"

    def test_the_old_fields_still_win_when_present(self):
        """Older responses, and whatever Vinted does next, keep working."""
        item = self.parse(brand_title="Patagonia", size_title="L", status="New with tags")
        assert item.brand_title == "Patagonia"
        assert item.size == "L"
        assert item.condition == "New with tags"

    def test_a_missing_item_box_is_not_a_crash(self):
        item = self.parse(item_box=None)
        assert item is not None
        assert item.brand_title == ""

    def test_the_hotness_signal_is_read_as_before(self):
        """100% coverage on the live service — the one field that had to survive."""
        assert self.parse().favourite_count == 4


class TestConditionVocabulary:
    """A condition we cannot read is indistinguishable from no condition.

    `passes_condition` rejects a blank condition, so an unrecognised vocabulary
    silently drops every listing. That is not hypothetical: without a `Locale`
    header the catalogue service answered "M · Très bon état", which ranks 0 and
    would have starved the feed with nothing in the log to say why.
    """

    def _raw(self, second_line):
        return {"id": 1, "price": {"amount": "50.0", "currency_code": "GBP"},
                "item_box": {"first_line": "Rab", "second_line": second_line}}

    def test_a_known_condition_is_read(self):
        item = VintedItem.from_json(self._raw("M · Very good"),
                                    base_url="https://x")
        assert item.size == "M"
        assert item.condition == "Very good"

    def test_an_unknown_vocabulary_is_announced(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            VintedItem.from_json(self._raw("M · Très bon état"), base_url="https://x")
        assert "No recognised condition" in caplog.text
        assert "Très bon état" in caplog.text

    def test_a_listing_with_no_second_line_is_not_flagged(self, caplog):
        """Plenty of listings genuinely have neither, and warning on those would
        bury the signal in noise."""
        import logging
        with caplog.at_level(logging.WARNING):
            VintedItem.from_json(self._raw(""), base_url="https://x")
        assert "No recognised condition" not in caplog.text


class TestListingUrl:
    """A card whose link does nothing is worse than a missing card.

    The catalogue service returns a bare path where the old endpoint returned a
    full URL. `hot.js` whitelists hrefs to http(s) before assigning one -- an XSS
    guard, and correct -- so a relative path means the anchor never gets an href
    at all. The listings render, look fine, and are unclickable.
    """

    def _raw(self, url):
        raw = {"id": 10012275111, "price": {"amount": "50.0", "currency_code": "GBP"},
               "item_box": {"first_line": "Rab", "second_line": "M · Very good"}}
        if url is not None:
            raw["url"] = url
        return raw

    def _url(self, url):
        return VintedItem.from_json(
            self._raw(url), base_url="https://www.vinted.co.uk").url

    def test_a_bare_path_is_made_absolute(self):
        assert self._url("/items/10012275111-arcteryx-jacket") == \
            "https://www.vinted.co.uk/items/10012275111-arcteryx-jacket"

    def test_an_absolute_url_is_left_alone(self):
        assert self._url("https://www.vinted.co.uk/items/1-a") == \
            "https://www.vinted.co.uk/items/1-a"

    def test_a_path_without_a_leading_slash_still_works(self):
        assert self._url("items/1-a") == "https://www.vinted.co.uk/items/1-a"

    def test_a_missing_url_falls_back_to_the_id(self):
        assert self._url(None) == "https://www.vinted.co.uk/items/10012275111"
        assert self._url("") == "https://www.vinted.co.uk/items/10012275111"

    def test_the_result_always_passes_an_http_whitelist(self):
        """The page's own test, applied here, because here is where it can be
        fixed without weakening the guard."""
        for candidate in (None, "", "/items/1-a", "items/1-a",
                          "https://www.vinted.co.uk/items/1-a"):
            assert self._url(candidate).startswith("https://")

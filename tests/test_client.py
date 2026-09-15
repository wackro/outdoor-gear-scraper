"""How the catalogue request is built.

This file exists because of a failure that no test would have caught and no log
line explained: the scraper sent 56 brand ids comma-joined into a single
`attribute_ids[brand]`, the service matched none of them, and it returned HTTP
200 with an empty list. Every category logged "fetched 0" for two merge cycles
while the endpoint worked perfectly for anyone who asked it properly.

So what is pinned here is the wire format, asserted against the encoder the
client actually uses. Offline: `update_url_params` is the function `curl_cffi`
calls on its way to building the URL, so this is the real thing, not a model of
it.
"""
import logging

import pytest
from curl_cffi.requests.utils import update_url_params

from src.config import load_config
from src.vinted.client import (
    BRANDS_PATH, CATALOG_PATH, LEGACY_CATALOG_PATH, VintedClient, VintedError,
    catalogue_host,
)


@pytest.fixture
def client():
    return VintedClient(load_config("config/config.example.yaml"))


class RecordingSession:
    """Captures the call instead of making it, and answers with no items."""

    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload if payload is not None else {"items": []}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}})
        return _Response(self.payload)


class _Response:
    status_code = 200
    url = "https://api.vinted.co.uk/svc-catalogue/items"
    headers: dict = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def query_of(call) -> str:
    return update_url_params(call["url"], call["params"])


class TestBrandEncoding:
    """The bug, pinned."""

    def test_each_brand_id_gets_its_own_key(self, client):
        session = RecordingSession()
        client._session = session
        client._get_page([319730, 90804, 2319], catalog_id=2052, page=1)

        url = query_of(session.calls[0])
        assert url.count("attribute_ids%5Bbrand%5D=") == 3
        for brand_id in (319730, 90804, 2319):
            assert f"attribute_ids%5Bbrand%5D={brand_id}" in url

    def test_the_ids_are_never_comma_joined(self, client):
        """The shape that returned an empty list with a 200."""
        session = RecordingSession()
        client._session = session
        client._get_page([1, 2, 3], catalog_id=2052, page=1)

        assert "1%2C2%2C3" not in query_of(session.calls[0])
        assert session.calls[0]["params"]["attribute_ids[brand]"] == [1, 2, 3]

    def test_a_single_brand_is_still_a_list(self, client):
        """One id worked by accident under the old encoding, which is why the
        probe kept reporting success while the scraper got nothing."""
        session = RecordingSession()
        client._session = session
        client._get_page([2319], catalog_id=2052, page=1)
        assert session.calls[0]["params"]["attribute_ids[brand]"] == [2319]

    def test_the_old_filter_names_are_gone(self, client):
        session = RecordingSession()
        client._session = session
        client._get_page([1], catalog_id=2052, page=1)
        params = session.calls[0]["params"]
        assert "brand_ids" not in params
        assert "catalog_ids" not in params
        assert params["attribute_ids[catalog]"] == 2052

    def test_both_entry_points_send_the_same_shape(self, client):
        """`fetch_items` is the daily scrape and `fetch_page` the poller; they
        share `_get_page`, and a fix that reached only one would leave alerts
        dead while the site looked healthy."""
        session = RecordingSession()
        client._session = session
        client.fetch_page([1, 2], catalog_id=2052)
        client.fetch_items([1, 2], catalog_id=2052)
        assert len(session.calls) >= 2
        for call in session.calls:
            assert call["params"]["attribute_ids[brand]"] == [1, 2]


class TestHeaders:
    def test_the_catalogue_request_states_its_locale(self, client):
        session = RecordingSession()
        client._session = session
        client._get_page([1], catalog_id=2052, page=1)

        headers = session.calls[0]["headers"]
        assert headers["Locale"] == "en-GB"
        assert headers["Accept-Language"].startswith("en-GB")
        assert headers["X-Next-App"] == "marketplace-web"

    def test_accept_language_goes_everywhere(self, client):
        """A browser sends it on every request; a client that sends it on one
        endpoint and not another is a pattern worth not having."""
        session = RecordingSession({"brands": []})
        client._session = session
        client.resolve_brand("rab")
        assert "Accept-Language" in session.calls[0]["headers"]

    def test_brands_does_not_get_the_marketplace_app_headers(self, client):
        """`Locale` belongs to the catalogue service. The brands endpoint is the
        older API and was never sent them."""
        session = RecordingSession({"brands": []})
        client._session = session
        client.resolve_brand("rab")
        assert "X-Next-App" not in session.calls[0]["headers"]


class TestHosts:
    def test_the_catalogue_goes_to_the_api_host(self, client):
        session = RecordingSession()
        client._session = session
        client._get_page([1], catalog_id=2052, page=1)
        assert session.calls[0]["url"].startswith(
            "https://api.vinted.co.uk/svc-catalogue/items")

    def test_brands_stayed_on_www(self, client):
        session = RecordingSession({"brands": []})
        client._session = session
        client.resolve_brand("rab")
        assert session.calls[0]["url"].startswith("https://www.vinted.co.uk/api/v2/brands")

    def test_the_legacy_path_is_kept_for_the_diagnostic_control(self):
        assert LEGACY_CATALOG_PATH == "/api/v2/catalog/items"
        assert CATALOG_PATH == "/svc-catalogue/items"
        assert BRANDS_PATH == "/api/v2/brands"
        assert catalogue_host("https://www.vinted.co.uk") == "https://api.vinted.co.uk"


class TestCurrencyGuard:
    """Currency now rides on a header, so silence is not consent."""

    def _raw(self, currency):
        price = {"amount": "50.0"}
        if currency:
            price["currency_code"] = currency
        return {"id": 1, "title": "Jacket", "price": price,
                "item_box": {"first_line": "Rab", "second_line": "M · Very good"}}

    def test_a_matching_currency_is_kept(self, client):
        assert len(client._parse([self._raw("GBP")])) == 1

    def test_another_currency_is_dropped(self, client):
        assert client._parse([self._raw("USD")]) == []

    def test_an_unstated_currency_is_dropped(self, client):
        """The old rule accepted it. With locale deciding the currency, an
        unlabelled price could be anything, and a wrong one does not fail --
        it joins ninety days of GBP history and skews every baseline on it."""
        assert client._parse([self._raw(None)]) == []

    def test_discarding_everything_says_what_it_discarded(self, client, caplog):
        """A silent zero is the failure that cost two merge cycles."""
        with caplog.at_level(logging.WARNING):
            client._parse([self._raw("USD"), self._raw("USD"), self._raw(None)])
        assert "Discarded all 3 listings" in caplog.text
        assert "USD x2" in caplog.text
        assert "(none stated) x1" in caplog.text

    def test_it_stays_quiet_when_something_survived(self, client, caplog):
        with caplog.at_level(logging.WARNING):
            client._parse([self._raw("GBP"), self._raw("USD")])
        assert "Discarded all" not in caplog.text


class TestEmptyBrandList:
    def test_it_refuses_to_fetch_the_whole_catalogue(self, client):
        """An empty list drops the filter key, so the request becomes the
        firehose. `run.py` already guards; the poller does not, and would have
        repeated it every cycle."""
        client._session = RecordingSession()
        with pytest.raises(VintedError, match="entire catalogue"):
            client._get_page([], catalog_id=2052, page=1)
        assert client._session.calls == []

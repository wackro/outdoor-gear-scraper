"""Vinted UK catalog client.

Vinted has no public API. This talks to the same internal JSON endpoint the
website uses. Two things make that work reliably:

  1. A session must first be bootstrapped by loading the homepage, which sets the
     anonymous cookies the API requires.
  2. Vinted fronts everything with DataDome, which fingerprints the TLS/JA3
     handshake. Plain `requests`/`httpx` get blocked quickly, so we use
     `curl_cffi` with browser impersonation to present a real browser fingerprint.

This is an undocumented endpoint and may change or block without notice — every
response is parsed defensively and failures are surfaced, not swallowed silently.
"""
from __future__ import annotations

import logging
import os
import random
import re
import time
import unicodedata

from curl_cffi import requests

from ..config import Config
from .models import VintedItem

log = logging.getLogger(__name__)

# The catalogue moved to its own host and path on 14 September 2026. Brands did
# not: it still answers from www. That split is what made the outage confusing --
# one endpoint 404'd at every parameter shape while another kept working on the
# same session, which reads like a retirement and was a relocation.
CATALOG_PATH = "/svc-catalogue/items"
BRANDS_PATH = "/api/v2/brands"

# The path the catalogue served from until 14 September 2026. Kept because the
# diagnostic needs a control: probing the *new* path on the *old* host proves
# nothing, and for one cycle that is exactly what it did.
LEGACY_CATALOG_PATH = "/api/v2/catalog/items"


def catalogue_host(base_url: str) -> str:
    """www.vinted.co.uk -> api.vinted.co.uk, where the catalogue now lives."""
    return base_url.replace("://www.", "://api.", 1)

# Top-level departments in the homepage catalog tree, used to tag each category
# node with the gender/section it belongs to.
DEPARTMENTS = ["Women", "Men", "Kids", "Home", "Electronics", "Entertainment", "Beauty", "Pet care"]
_TREE_NODE = re.compile(r'"id":(\d+),"title":"([^"]+)","url":"/catalog/')


def _norm(text: str) -> str:
    """Fold accents/case/punctuation for tolerant brand-name matching.

    e.g. "Fjällräven" -> "fjallraven", "Arc'teryx" -> "arcteryx".
    """
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(c for c in stripped.lower() if c.isalnum())


class VintedError(RuntimeError):
    """Raised when the catalog cannot be fetched after retries."""


class VintedBlocked(VintedError):
    """Raised when Vinted rate-limited or challenged us, rather than failing.

    Distinct from a generic error because the right response is different: a
    transient network blip should be retried promptly, but a 429 or a DataDome
    challenge means backing off hard. Callers must be able to tell them apart
    without pattern-matching on an error string — the message includes the query
    params, and a brand id containing "403" would otherwise look like a block.
    """


class VintedClient:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = config.base_url
        self._session: requests.Session | None = None

    # -- session management --------------------------------------------------

    def _new_session(self) -> requests.Session:
        session = requests.Session(impersonate=self.config.scrape.impersonate)
        proxy = os.environ.get("PROXY_URL")
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        return session

    def bootstrap(self) -> None:
        """Load the homepage to obtain the anonymous session cookies."""
        self._session = self._new_session()
        resp = self._session.get(self.base_url, timeout=30)
        if resp.status_code >= 400:
            raise VintedError(f"Session bootstrap failed (HTTP {resp.status_code}).")
        log.info("Bootstrapped Vinted session (%d cookies).", len(self._session.cookies))

    def _ensure_session(self) -> requests.Session:
        if self._session is None:
            self.bootstrap()
        assert self._session is not None
        return self._session

    # -- fetching ------------------------------------------------------------

    def _service_headers(self) -> dict[str, str]:
        """What the marketplace-web app sends the catalogue service.

        `Locale` is the load-bearing one. The old www endpoint inferred locale
        from the host; api.vinted.co.uk does not, so without this it answers in
        a locale of its own choosing -- observed as French condition strings and
        dollar prices, neither of which any downstream code can read.
        """
        return {
            "Locale": self.config.locale,
            "X-Next-App": "marketplace-web",
            "Platform": "web",
        }

    def _request(self, params: dict, path: str = CATALOG_PATH, result_key: str = "items",
                 host: str | None = None, extra_headers: dict | None = None) -> list[dict]:
        """Call a Vinted API endpoint with retries/backoff; return the result list."""
        headers = {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            # Every request, not just the catalogue's: a browser always sends it,
            # and a client that doesn't stands out.
            "Accept-Language": f"{self.config.locale},{self.config.locale.split('-')[0]};q=0.9",
            **(extra_headers or {}),
        }
        base = host or self.base_url

        last_error: Exception | None = None
        blocked = False
        for attempt in range(1, self.config.scrape.max_retries + 1):
            try:
                session = self._ensure_session()  # may (re-)bootstrap
                resp = session.get(
                    base + path,
                    params=params,
                    headers=headers,
                    timeout=30,
                )
            except Exception as exc:  # network / curl / bootstrap errors
                last_error = exc
                self._session = None  # force a fresh session on the next attempt
                log.warning("Request error (attempt %d): %s", attempt, exc)
            else:
                if resp.status_code in (401, 403):
                    # Session likely expired or was challenged — rebuild and retry.
                    log.warning("HTTP %d; re-bootstrapping session.", resp.status_code)
                    self._session = None
                    blocked = resp.status_code == 403
                    last_error = VintedError(f"HTTP {resp.status_code}")
                elif resp.status_code == 429:
                    blocked = True
                    last_error = VintedError("HTTP 429 (rate limited)")
                    log.warning("Rate limited (attempt %d).", attempt)
                elif resp.status_code >= 400:
                    raise VintedError(f"HTTP {resp.status_code} for {resp.url}")
                else:
                    try:
                        return resp.json().get(result_key) or []
                    except ValueError as exc:
                        last_error = exc
                        log.warning("Non-JSON response (attempt %d).", attempt)

            if attempt < self.config.scrape.max_retries:
                time.sleep(2 ** attempt)  # exponential backoff: 2s, 4s, ...

        error_type = VintedBlocked if blocked else VintedError
        raise error_type(f"Request failed (params={params}): {last_error}")

    def _get_page(self, brand_ids: list[int], catalog_id: int, page: int) -> list[dict]:
        """One page of the catalogue.

        Filters moved into an `attribute_ids[...]` shape when the service split
        out; `catalog_ids` and `brand_ids` are no longer recognised.

        Brand ids are comma-joined into one value. This was measured, not
        assumed: probed side by side with all 56 of our ids, the comma-joined
        value returns 20 listings and one repeated key per id
        (`attribute_ids[brand]=1&attribute_ids[brand]=2`) returns **HTTP 400**.

        Repeated keys are what Vintrack sends and what this code briefly sent on
        the strength of that, which cost a run. Their client works, so the
        service presumably accepts both at the handful of ids a person filters
        by in a browser; at 56 it does not. Whatever the boundary is, the shape
        that answers is the one we send.
        """
        if not brand_ids:
            # Otherwise the filter is an empty value, which asks for the whole
            # unfiltered catalogue -- a slow, conspicuous request whose every
            # result the brand match then discards. `run.py` already refuses
            # this; the poller has no such check and would repeat it every cycle.
            raise VintedError("No brand ids to filter on; refusing to fetch the "
                              "entire catalogue.")
        return self._request(
            {
                "page": page,
                "per_page": self.config.scrape.per_page,
                "order": self.config.scrape.order,
                "attribute_ids[catalog]": catalog_id,
                "attribute_ids[brand]": ",".join(str(b) for b in brand_ids),
                # Inert on this service -- locale decides the currency -- but
                # harmless, and it still documents what we expect to get back.
                "currency": self.config.currency,
            },
            host=catalogue_host(self.base_url),
            extra_headers=self._service_headers(),
        )

    def resolve_brand(self, search_text: str) -> tuple[int, str] | None:
        """Resolve a brand name to its Vinted (id, canonical title).

        `GET /api/v2/brands?keyword=<name>` returns the matching brands. We pick
        the one whose title equals the search (accent/case-folded); failing that,
        the first close match. Returns None if nothing matches.
        """
        brands = self._request({"keyword": search_text}, path=BRANDS_PATH, result_key="brands")
        target = _norm(search_text)

        for brand in brands:
            if brand.get("id") and _norm(brand.get("title", "")) == target:
                return int(brand["id"]), str(brand.get("title") or "")
        for brand in brands:
            title = _norm(brand.get("title", ""))
            if brand.get("id") and title and (target in title or title in target):
                return int(brand["id"]), str(brand.get("title") or "")
        return None

    def resolve_brand_id(self, search_text: str) -> int | None:
        result = self.resolve_brand(search_text)
        return result[0] if result else None

    def fetch_catalog_tree(self) -> list[tuple[int, str, str]]:
        """Return the catalog tree as (catalog_id, title, department) tuples.

        Vinted has no catalog-tree API, but the homepage server-renders the tree
        as escaped JSON. We unescape it, then tag each category node with the
        nearest preceding department node so we know its gender/section.
        """
        session = self._ensure_session()
        html = session.get(self.base_url + "/", timeout=30).text
        text = html.replace('\\"', '"').replace("\\/", "/").replace("\\u0026", "&")

        dept_offsets: dict[str, int] = {}
        for name in DEPARTMENTS:
            m = re.search(r'"title":"' + re.escape(name) + r'","url":"/catalog/', text)
            if m:
                dept_offsets[name] = m.start()

        def department_for(offset: int) -> str:
            cands = [(o, n) for n, o in dept_offsets.items() if o <= offset]
            return max(cands)[1] if cands else ""

        nodes: list[tuple[int, str, str]] = []
        for m in _TREE_NODE.finditer(text):
            nodes.append((int(m.group(1)), m.group(2), department_for(m.start())))
        return nodes

    def _parse(self, raw_items: list[dict]) -> list[VintedItem]:
        """Turn raw catalog entries into items, skipping anything unusable.

        The currency test demands a positive match rather than merely the
        absence of a contradiction. Currency now depends on a request *header*
        rather than on which host we asked, so "no currency stated" is no longer
        a safe bet on GBP -- and a mis-stated price does not fail, it quietly
        joins ninety days of GBP history and skews every baseline and deal built
        on it. Dropping the item costs one run; trusting it costs the database.

        Rejections are counted and reported, because a silent zero is precisely
        the failure that cost two merge cycles here. A run that returns nothing
        should always be able to say what it threw away.
        """
        items: list[VintedItem] = []
        unparseable = 0
        wrong_currency: dict[str, int] = {}
        for raw in raw_items:
            item = VintedItem.from_json(raw, base_url=self.base_url)
            if item is None:
                unparseable += 1
                continue
            if item.currency != self.config.currency:
                seen = item.currency or "(none stated)"
                wrong_currency[seen] = wrong_currency.get(seen, 0) + 1
                continue
            items.append(item)

        if raw_items and not items:
            log.warning(
                "Discarded all %d listings: %d unparseable, %d in the wrong "
                "currency (wanted %s, saw %s).",
                len(raw_items), unparseable, sum(wrong_currency.values()),
                self.config.currency,
                ", ".join(f"{name} x{n}" for name, n in sorted(wrong_currency.items()))
                or "none",
            )
        return items

    def fetch_items(self, brand_ids: list[int], catalog_id: int) -> list[VintedItem]:
        """Fetch newest items for a set of brands within a category.

        Passing all watched brands in one request keeps the request count to one
        per category rather than one per brand+category. The service takes them
        as repeated `attribute_ids[brand]` keys; see `_get_page`.
        """
        items: list[VintedItem] = []
        for page in range(1, self.config.scrape.max_pages_per_query + 1):
            raw_items = self._get_page(brand_ids, catalog_id, page)
            if not raw_items:
                break
            items.extend(self._parse(raw_items))
            if len(raw_items) < self.config.scrape.per_page:
                break  # last page
            self.throttle()
        return items

    def fetch_page(
        self, brand_ids: list[int], catalog_id: int, page: int = 1
    ) -> list[VintedItem]:
        """Fetch exactly one page — the hot path's unit of work.

        The daily scrape walks several pages per category to build price history.
        The poller instead wants a single cheap read it can repeat every minute,
        so it controls pagination itself rather than inheriting `max_pages_per_query`.
        """
        return self._parse(self._get_page(brand_ids, catalog_id, page))

    def reset_session(self) -> None:
        """Drop the current session so the next request bootstraps a fresh one.

        Long-running polling accumulates a stale cookie jar and a session that has
        been talking to DataDome for hours; periodically starting over looks far
        more like a normal browser than one immortal session.
        """
        self._session = None

    def throttle(self) -> None:
        """Sleep a randomised, polite delay (between pages and between queries)."""
        delay = random.uniform(
            self.config.scrape.min_delay_sec, self.config.scrape.max_delay_sec
        )
        time.sleep(delay)


def _smoke_test() -> None:
    """Manual smoke test: fetch one brand+category and print a few items."""
    logging.basicConfig(level=logging.INFO)
    from ..config import load_config

    config = load_config()
    client = VintedClient(config)
    brand = config.brands[0]
    brand_id = client.resolve_brand_id(brand.search_text) or brand.id
    category = config.categories[0]
    catalog_id = category.id
    print(f"Fetching {brand.name} / {category.name} (brand={brand_id}, catalog={catalog_id})")
    items = client.fetch_items([brand_id], catalog_id)
    print(f"Got {len(items)} items")
    for item in items[:5]:
        print(f"  £{item.price:>7.2f}  {item.brand_title:<15} {item.size:<8} "
              f"{item.condition:<12} {item.title[:34]}")


if __name__ == "__main__":
    _smoke_test()

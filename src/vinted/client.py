"""Vinted UK catalog client.

Vinted has no public API. This talks to the same internal JSON endpoint the
website uses (`/api/v2/catalog/items`). Two things make that work reliably:

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

CATALOG_PATH = "/api/v2/catalog/items"
BRANDS_PATH = "/api/v2/brands"

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

    def _request(self, params: dict, path: str = CATALOG_PATH, result_key: str = "items") -> list[dict]:
        """Call a Vinted API endpoint with retries/backoff; return the result list."""
        headers = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}

        last_error: Exception | None = None
        for attempt in range(1, self.config.scrape.max_retries + 1):
            try:
                session = self._ensure_session()  # may (re-)bootstrap
                resp = session.get(
                    self.base_url + path,
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
                    last_error = VintedError(f"HTTP {resp.status_code}")
                elif resp.status_code == 429:
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

        raise VintedError(f"Request failed (params={params}): {last_error}")

    def _get_page(self, brand_id: int, catalog_id: int, page: int) -> list[dict]:
        return self._request(
            {
                "page": page,
                "per_page": self.config.scrape.per_page,
                "order": self.config.scrape.order,
                "catalog_ids": catalog_id,
                "brand_ids": brand_id,
                "currency": self.config.currency,
            }
        )

    def resolve_brand_id(self, search_text: str) -> int | None:
        """Resolve a brand name to its Vinted brand id via the brand endpoint.

        `GET /api/v2/brands?keyword=<name>` returns the matching brands. We pick
        the one whose title equals the search (accent/case-folded); failing that,
        the first close match. Returns None if nothing matches, so callers can
        fall back to a configured id.
        """
        brands = self._request({"keyword": search_text}, path=BRANDS_PATH, result_key="brands")
        target = _norm(search_text)

        for brand in brands:
            if brand.get("id") and _norm(brand.get("title", "")) == target:
                return int(brand["id"])
        for brand in brands:
            title = _norm(brand.get("title", ""))
            if brand.get("id") and title and (target in title or title in target):
                return int(brand["id"])
        return None

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

    def fetch_items(self, brand_id: int, catalog_id: int) -> list[VintedItem]:
        """Fetch newest items for a brand+category across the configured page cap."""
        items: list[VintedItem] = []
        for page in range(1, self.config.scrape.max_pages_per_query + 1):
            raw_items = self._get_page(brand_id, catalog_id, page)
            if not raw_items:
                break
            for raw in raw_items:
                item = VintedItem.from_json(raw, base_url=self.base_url)
                if item is None:
                    continue
                if item.currency and item.currency != self.config.currency:
                    continue  # ignore listings priced in another currency
                items.append(item)
            if len(raw_items) < self.config.scrape.per_page:
                break  # last page
            self.throttle()
        return items

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
    items = client.fetch_items(brand_id, catalog_id)
    print(f"Got {len(items)} items")
    for item in items[:5]:
        print(f"  £{item.price:>7.2f}  {item.brand_title:<15} {item.size:<8} "
              f"{item.condition:<12} {item.title[:34]}")


if __name__ == "__main__":
    _smoke_test()

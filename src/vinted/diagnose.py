"""Ask the live API what it actually does, rather than what we assume.

`VintedClient._request` is built for the happy path: it retries, re-bootstraps on
401/403, and on anything else raises `VintedError(f"HTTP {status}")` -- throwing
the response body away. That is the right trade for the scraper and useless for
diagnosis, because when an endpoint starts refusing us the body is the evidence.
A JSON 404 from Vinted means the endpoint moved; an HTML page or a DataDome
header means a bot wall wearing a 404 as a disguise. Those need opposite fixes.

So this issues raw requests through the same session and reports what came back,
never raising and never retrying.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .client import VintedClient

# Headers worth surfacing: the first two name the bot wall if one is in the way,
# the third separates "JSON error from the API" from "HTML page from a proxy".
TELLTALE_HEADERS = ("x-datadome", "x-datadome-cid", "content-type", "server")

BODY_EXCERPT_CHARS = 300


@dataclass
class Probe:
    """One request and what it produced. `error` is set only if it never landed."""
    name: str
    asks: str                       # the hypothesis this isolates
    url: str = ""
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    item_count: int | None = None   # parsed from JSON when the shape is familiar
    has_favourite_count: bool = False
    # field -> % of returned items carrying it. The response shape drifts, and a
    # field present on one listing but absent on most is a trap: it looks fine
    # in a sample and starves the signal in production.
    coverage: dict[str, float] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def verdict(self) -> str:
        if self.error:
            return f"did not complete: {self.error}"
        if self.ok:
            if self.item_count:
                counter = "with" if self.has_favourite_count else "**no**"
                plural = "" if self.item_count == 1 else "s"
                return (f"OK, {self.item_count} item{plural}, "
                        f"{counter} favourite_count")
            if self.item_count == 0:
                return "OK, but zero items"
            return "OK"
        return f"HTTP {self.status}"

    @property
    def usable(self) -> bool:
        """Could the scraper actually run on this?

        Not the same as a 200. A replacement endpoint that returns listings
        without `favourite_count` is no use at all: that counter is the entire
        hotness signal, so such a find would be a dead end wearing the costume
        of a solution.
        """
        return bool(self.ok and self.item_count and self.has_favourite_count)


# The fields the scraper and the hot path actually consume. `favourite_count` is
# first because it is not merely useful: it is the entire hotness signal, and an
# endpoint that omits it cannot drive alerts at all.
FIELDS_WE_NEED = ("favourite_count", "brand_title", "size_title", "status",
                  "view_count", "photo")


def _coverage(items: list[dict]) -> dict[str, float]:
    """Share of items carrying each field we depend on.

    Presence on one listing is not enough to build on -- Vinted legitimately
    omits some of these per listing -- so this reports how often we can rely on
    it, which is the number that decides whether a response is usable.
    """
    if not items:
        return {}
    return {
        field_name: 100.0 * sum(1 for i in items if i.get(field_name) is not None) / len(items)
        for field_name in FIELDS_WE_NEED
    }


def _looks_like_a_bot_wall(probe: Probe) -> bool:
    """A block dressed as something else.

    DataDome answers with its own headers, or with an HTML challenge page where
    the API would have sent JSON. Either means the request never reached Vinted's
    application, so no amount of fixing parameters will help.
    """
    if any(key.startswith("x-datadome") for key in probe.headers):
        return True
    content_type = probe.headers.get("content-type", "")
    return "html" in content_type.lower() and "/api/" in probe.url


def run_probe(client: VintedClient, name: str, asks: str, path: str,
              params: dict | None = None, *, headers: dict | None = None) -> Probe:
    """Issue one raw request. Never raises: a failed probe is a result.

    `path` may be absolute. That is not hypothetical tidiness: the catalogue
    lives on a different host from the rest of the API now.
    """
    url = path if path.startswith("http") else client.base_url + path
    probe = Probe(name=name, asks=asks, url=url)
    try:
        session = client._ensure_session()
        response = session.get(
            url,
            params=params or {},
            headers={"Accept": "application/json",
                     "X-Requested-With": "XMLHttpRequest", **(headers or {})},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 -- reporting is the whole job
        probe.error = f"{type(exc).__name__}: {exc}"
        return probe

    probe.url = str(response.url)
    probe.status = response.status_code
    probe.headers = {
        key: value for key, value in
        {k.lower(): v for k, v in response.headers.items()}.items()
        if key in TELLTALE_HEADERS
    }
    probe.body = (response.text or "")[:BODY_EXCERPT_CHARS].replace("\n", " ").strip()
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 -- a non-JSON body is itself a finding
        return probe
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        items = [i for i in payload["items"] if isinstance(i, dict)]
        probe.item_count = len(payload["items"])
        probe.coverage = _coverage(items)
        probe.has_favourite_count = probe.coverage.get("favourite_count", 0.0) > 0
    return probe


def diagnose(client: VintedClient, *, catalog_id: int, brand_ids: list[int],
             catalog_path: str, brands_path: str) -> list[Probe]:
    """Work from 'can we reach it at all' up to the exact failing request.

    Ordered so that the first failure is the most informative one: if the
    homepage is fine but every /api/v2 path 404s, the API moved; if only the
    fully-loaded catalog call fails, it is the parameters.
    """
    all_brands = ",".join(str(b) for b in brand_ids)
    one_brand = str(brand_ids[0]) if brand_ids else ""
    common = {"page": 1, "per_page": 96, "order": "newest_first", "currency": client.config.currency}

    return [
        run_probe(client, "homepage", "is Vinted reachable at all?", "/"),
        run_probe(client, "brands", "does a known-good /api/v2 endpoint still work?",
                  brands_path, {"keyword": "rab"}),
        run_probe(client, "catalog: bare", "does the catalog path exist, with no filters?",
                  catalog_path, {"page": 1, "per_page": 1}),
        run_probe(client, "catalog: live request", "reproduce exactly what the scraper sends",
                  catalog_path, {**common, "catalog_ids": catalog_id, "brand_ids": all_brands}),
        run_probe(client, "catalog: one brand", "is it the number of brand_ids, or the URL length?",
                  catalog_path, {**common, "catalog_ids": catalog_id, "brand_ids": one_brand}),
        run_probe(client, "catalog: no brands", "is brand_ids the rejected parameter?",
                  catalog_path, {**common, "catalog_ids": catalog_id}),
        run_probe(client, "catalog: no category", "is catalog_ids rejected? (also the firehose)",
                  catalog_path, {**common, "brand_ids": all_brands}),
    ]


def interpret(probes: list[Probe]) -> str:
    """The one line worth reading first, on a phone, before the table."""
    by_name = {p.name: p for p in probes}
    catalog = [p for p in probes if p.name.startswith("catalog")]

    if any(_looks_like_a_bot_wall(p) for p in probes):
        return ("**Blocked, not broken.** Something answered with a DataDome header or "
                "an HTML page where JSON was expected, so the requests are not reaching "
                "Vinted's API. The fix is anti-detection, not the endpoint.")

    if not by_name.get("homepage", Probe("", "")).ok:
        return "**Vinted is unreachable from the runner.** Nothing below means much until that is true."

    if all(p.ok for p in catalog):
        return ("**Everything works now.** The failure was transient. Worth hardening "
                "`client._request`, which gives a 404 no retry at all.")

    if by_name.get("brands", Probe("", "")).ok and not any(p.ok for p in catalog):
        return ("**The catalog endpoint is gone.** /api/v2/brands still answers on the same "
                "session, so this is not auth and not a block: the path itself no longer "
                "serves. Next step is finding what replaced it.")

    working = [p.name for p in catalog if p.ok]
    if working:
        return (f"**It is the parameters, not the endpoint.** These still work: "
                f"{', '.join(working)}. Compare them against the failing ones below — "
                f"the difference is the fix.")

    return "**Mixed result.** Read the table; the pattern is not one of the usual ones."


# --- finding the endpoint that replaced the one that vanished ----------------

# Any service route, as it appears in server-rendered JSON or a JS bundle.
#
# This used to match only `/api/v<n>/...`, which is why the last hunt reported
# "nothing found" with total confidence while the replacement was sitting in
# plain sight: Vinted moved the catalogue to `/svc-catalogue/items`, and a
# pattern that only knows about /api/ can never see it. Keep it broad.
API_PATH = re.compile(r"/(?:api/v\d+|svc-[a-z0-9-]+)/[A-Za-z0-9_/-]{2,60}")

# <script src="..."> — the fetch calls live in the bundle, not the markup.
SCRIPT_SRC = re.compile(r'<script[^>]+src="([^"]+\.js[^"]*)"')

# Bundles run to megabytes and there are dozens. Read a bounded slice of a few
# of the most promising, rather than pulling the whole application down.
MAX_BUNDLES = 3
MAX_BUNDLE_BYTES = 2_000_000

# Tried alongside whatever discovery turns up, so the report is useful even when
# the page gives nothing away. Ordered by how plausible a successor each is.
SERVICE_CATALOGUE_PATH = "/svc-catalogue/items"

CANDIDATE_PATHS = (
    "/api/v2/items",
    "/api/v2/catalog/items",        # the dead one, as the control
    "/api/v3/catalog/items",
    "/api/v2/catalog/search",
    "/api/v2/search/items",
)

# A UUID sitting near the literal CSRF_TOKEN in the server-rendered page.
CSRF_IN_PAGE = re.compile(
    r"CSRF_TOKEN.{0,60}?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I | re.S,
)


def warmup(client: VintedClient) -> tuple[dict[str, str], list[str]]:
    """Collect the headers the catalogue service now demands.

    It wants `X-Anon-Id` and `X-Csrf-Token`. Both fall out of the homepage
    request the client already makes to get its cookies -- the anon id from a
    response header, the token from the page body -- so this costs nothing extra
    beyond reading what we were already fetching.

    Returns (headers, notes). Missing either is a finding, not an error: the
    probe should still fire so we can see what the service says without them.
    """
    notes: list[str] = []
    headers: dict[str, str] = {}
    try:
        session = client._ensure_session()
        response = session.get(client.base_url + "/", timeout=30)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"warmup request failed: {type(exc).__name__}: {exc}")
        return headers, notes

    anon = {k.lower(): v for k, v in response.headers.items()}.get("x-anon-id")
    if anon:
        headers["X-Anon-Id"] = anon
        notes.append("X-Anon-Id: found in the response headers")
    else:
        notes.append("X-Anon-Id: **not offered** by the homepage response")

    match = CSRF_IN_PAGE.search(response.text or "")
    if match:
        headers["X-Csrf-Token"] = match.group(1)
        notes.append("X-Csrf-Token: found in the page body")
    else:
        notes.append("X-Csrf-Token: **not found** in the page body")
    return headers, notes

# Paths that could plausibly serve listings, ranked ahead of the rest.
INTERESTING = ("catalog", "catalogue", "item", "search", "feed")


def catalogue_host(base_url: str) -> str:
    """Where the catalogue service lives, given the site host.

    Vinted split it out onto its own host: www.vinted.co.uk serves the site and
    /api/v2/brands, api.vinted.co.uk serves the catalogue. That split is exactly
    why /api/v2/brands kept answering while every catalogue request 404'd.
    """
    return base_url.replace("://www.", "://api.", 1)


def _rank(path: str) -> tuple[int, str]:
    """Listing-ish paths first; otherwise alphabetical, so output is stable."""
    return (0 if any(word in path for word in INTERESTING) else 1, path)


def _unescape(text: str) -> str:
    """Server-rendered JSON arrives escaped inside the HTML.

    Same treatment `fetch_catalog_tree` applies to recover the category tree.
    """
    return text.replace('\\"', '"').replace("\\/", "/").replace("\\u0026", "&")


def discover_api_paths(client: VintedClient, *, page_path: str = "/catalog") -> tuple[list[str], list[str]]:
    """Read the API paths the site itself uses. Returns (paths, notes).

    A catalog page rather than the homepage: it is the page whose data we want,
    so whatever serves it is named in its payload or in the bundle it loads.

    Never raises -- discovery failing is a reportable outcome, not a crash.
    """
    notes: list[str] = []
    found: set[str] = set()

    try:
        session = client._ensure_session()
        response = session.get(client.base_url + page_path, timeout=30)
        html = response.text or ""
        notes.append(f"{page_path} returned HTTP {response.status_code}, {len(html):,} bytes")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"could not fetch {page_path}: {type(exc).__name__}: {exc}")
        return [], notes

    text = _unescape(html)
    in_page = set(API_PATH.findall(text))
    found |= in_page
    notes.append(f"{len(in_page)} API path(s) named in the page itself")

    bundles = SCRIPT_SRC.findall(html)
    notes.append(f"{len(bundles)} script bundle(s) referenced; reading up to {MAX_BUNDLES}")
    for src in bundles[:MAX_BUNDLES]:
        url = src if src.startswith("http") else client.base_url + src
        try:
            body = session.get(url, timeout=30).text or ""
        except Exception as exc:  # noqa: BLE001
            notes.append(f"bundle {src[:60]} failed: {type(exc).__name__}")
            continue
        clipped = body[:MAX_BUNDLE_BYTES]
        hits = set(API_PATH.findall(clipped))
        found |= hits
        notes.append(f"bundle {src.rsplit('/', 1)[-1][:40]}: {len(hits)} path(s) "
                     f"in the first {len(clipped):,} bytes")

    return sorted(found, key=_rank), notes


def hunt_for_replacement(client: VintedClient, *, catalog_id: int,
                         brand_ids: list[int]) -> tuple[list[Probe], list[str]]:
    """Probe every candidate successor, discovered or guessed.

    Each is asked the question the scraper needs answered -- newest items for one
    category -- so a candidate that answers but cannot do that job is visibly
    not the answer.
    """
    discovered, notes = discover_api_paths(client)
    auth, warm_notes = warmup(client)
    notes = warm_notes + notes

    probes = []

    # The prime candidate, and the reason this pass exists. Vintrack's
    # 14 Sep fix moved to api.<domain>/svc-catalogue/items with the filters
    # restructured into attribute_ids[...], so this is not a guess -- it is
    # somebody else's working code, asked to prove itself against our brands.
    service_url = catalogue_host(client.base_url) + SERVICE_CATALOGUE_PATH
    service_params = {
        "page": 1, "per_page": 20, "order": "newest_first",
        "currency": client.config.currency,
        "attribute_ids[catalog]": catalog_id,
    }
    if brand_ids:
        service_params["attribute_ids[brand]"] = str(brand_ids[0])
    probes.append(run_probe(
        client, "svc-catalogue/items",
        "the endpoint Vintrack moved to on 14 Sep",
        service_url, service_params, headers=auth,
    ))

    # The same service without the auth headers, so we learn whether they are
    # actually required or merely what somebody else happened to send.
    probes.append(run_probe(
        client, "svc-catalogue/items (no auth headers)",
        "are X-Anon-Id and X-Csrf-Token really needed?",
        service_url, service_params,
    ))

    # Everything else, still asked the old way, as the control.
    params = {
        "page": 1, "per_page": 5, "order": "newest_first",
        "currency": client.config.currency, "catalog_ids": catalog_id,
    }
    if brand_ids:
        params["brand_ids"] = str(brand_ids[0])
    for path in dict.fromkeys([*discovered, *CANDIDATE_PATHS]):
        source = "found on the page" if path in discovered else "educated guess"
        probes.append(run_probe(client, path, source, path, params, headers=auth))
    return probes, notes


def summarise_hunt(probes: list[Probe]) -> str:
    """The line that decides what happens next."""
    usable = [p for p in probes if p.usable]
    if usable:
        return (f"**Found a replacement: `{usable[0].name}`.** It returns listings "
                f"carrying `favourite_count`, which is everything the scraper and "
                f"the hot path need. Repoint `CATALOG_PATH` at it.")

    answering = [p for p in probes if p.ok and p.item_count]
    if answering:
        return (f"**Something answers, but it is not enough.** `{answering[0].name}` "
                f"returns listings with no `favourite_count` — the entire hotness "
                f"signal. Repointing at it would restore the site and leave alerts "
                f"permanently dead, so this needs a decision, not a patch.")

    return ("**No replacement found.** Nothing discovered on the page or guessed at "
            "serves listings. The JSON API looks closed to us; parsing the "
            "server-rendered catalog HTML is the remaining route.")

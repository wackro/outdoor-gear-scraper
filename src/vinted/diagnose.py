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
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def verdict(self) -> str:
        if self.error:
            return f"did not complete: {self.error}"
        if self.ok:
            if self.item_count is not None:
                return f"OK, {self.item_count} items"
            return "OK"
        return f"HTTP {self.status}"


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
              params: dict | None = None) -> Probe:
    """Issue one raw request. Never raises: a failed probe is a result."""
    probe = Probe(name=name, asks=asks, url=client.base_url + path)
    try:
        session = client._ensure_session()
        response = session.get(
            client.base_url + path,
            params=params or {},
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
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
        probe.item_count = len(payload["items"])
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

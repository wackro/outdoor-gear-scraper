"""Resolve configured brand names to Vinted brand ids, with a committed cache.

The user configures brands by *name*; this maps each to Vinted's numeric brand
id at scrape time so nobody has to hand-manage ids. Resolutions are cached in a
small JSON file (committed to the repo) so we only hit the network once per
brand, and so a later Vinted change that breaks resolution can't wipe out ids we
already know. Resolution order per brand:

    1. cache hit                      -> use it
    2. resolve by name over network   -> use + cache it
    3. configured `id` fallback       -> use it
    4. otherwise                      -> skip the brand (logged)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import Brand
from .client import VintedClient, VintedError

log = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "brand_ids.json"


def _load_cache(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        return {k: int(v) for k, v in json.loads(path.read_text()).items()}
    except (ValueError, OSError):
        log.warning("Could not read brand-id cache at %s; ignoring.", path)
        return {}


def _save_cache(path: Path, cache: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(cache.items())), indent=2) + "\n")


def resolve_brand_ids(
    client: VintedClient,
    brands: list[Brand],
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> dict[str, int]:
    """Return {brand_name: brand_id} for every brand we could resolve."""
    cache = _load_cache(cache_path)
    resolved: dict[str, int] = {}
    changed = False

    for brand in brands:
        if brand.name in cache:
            resolved[brand.name] = cache[brand.name]
            continue

        brand_id: int | None = None
        try:
            brand_id = client.resolve_brand_id(brand.search_text)
        except VintedError as exc:
            log.warning("Brand resolution failed for %s: %s", brand.name, exc)

        if brand_id is None:
            brand_id = brand.id  # configured fallback (may be None)
            if brand_id is not None:
                log.info("Using configured id %s for %s (resolution found nothing).",
                         brand_id, brand.name)
        else:
            log.info("Resolved %s -> brand id %s", brand.name, brand_id)

        if brand_id is None:
            log.warning("Could not resolve a brand id for '%s'; skipping it.", brand.name)
            continue

        resolved[brand.name] = brand_id
        if cache.get(brand.name) != brand_id:
            cache[brand.name] = brand_id
            changed = True

    if changed:
        _save_cache(cache_path, cache)
        log.info("Updated brand-id cache (%d brands).", len(cache))

    return resolved

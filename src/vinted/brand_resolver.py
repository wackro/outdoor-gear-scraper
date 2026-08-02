"""Resolve configured brand names to Vinted brand ids + canonical titles.

Brands are configured by name; this maps each to Vinted's numeric brand id (used
to filter catalog queries) and captures the canonical brand title (used to map a
returned item back to which watched brand it belongs to, since catalog items
carry only a `brand_title` string, not a brand id).

Cached in data/brand_ids.json as {name: {"id": int, "title": str}} so we only hit
the network once per brand. Resolution order per brand: cache hit -> resolve by
name -> configured `id` fallback -> skip (logged).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Brand
from .client import VintedClient, VintedError, _norm

log = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "brand_ids.json"


@dataclass
class BrandResolution:
    ids: dict[str, int] = field(default_factory=dict)          # brand name -> id
    name_by_title: dict[str, str] = field(default_factory=dict)  # norm(title) -> brand name


def _load_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        log.warning("Could not read brand-id cache at %s; ignoring.", path)
        return {}
    cache: dict[str, dict] = {}
    for name, value in raw.items():
        if isinstance(value, dict):  # current format
            cache[name] = {"id": int(value["id"]), "title": value.get("title")}
        else:                        # legacy {name: int}
            cache[name] = {"id": int(value), "title": None}
    return cache


def _save_cache(path: Path, cache: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(cache.items())), indent=2) + "\n")


def resolve_brands(
    client: VintedClient,
    brands: list[Brand],
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> BrandResolution:
    cache = _load_cache(cache_path)
    result = BrandResolution()
    changed = False

    for brand in brands:
        entry = cache.get(brand.name)
        if entry and entry.get("id") is not None:
            brand_id, title = entry["id"], entry.get("title")
        else:
            resolved = None
            try:
                resolved = client.resolve_brand(brand.search_text)
            except VintedError as exc:
                log.warning("Brand resolution failed for %s: %s", brand.name, exc)
            if resolved is not None:
                brand_id, title = resolved
                log.info("Resolved %s -> id %s (%s)", brand.name, brand_id, title)
            else:
                brand_id, title = brand.id, None
                if brand_id is not None:
                    log.info("Using configured id %s for %s.", brand_id, brand.name)
            if brand_id is not None:
                cache[brand.name] = {"id": brand_id, "title": title}
                changed = True

        if brand_id is None:
            log.warning("Could not resolve a brand id for '%s'; skipping it.", brand.name)
            continue

        result.ids[brand.name] = brand_id
        # Map item brand_title -> our brand name. Prefer the canonical title, and
        # add the configured name/search as fallbacks in case titles differ.
        if title:
            result.name_by_title[_norm(title)] = brand.name
        result.name_by_title.setdefault(_norm(brand.search_text), brand.name)
        result.name_by_title.setdefault(_norm(brand.name), brand.name)

    if changed:
        _save_cache(cache_path, cache)
        log.info("Updated brand-id cache (%d brands).", len(cache))

    return result

"""Resolve configured categories (gender + name) to Vinted catalog ids.

Vinted has no catalog-tree API, so we read the tree the homepage server-renders
(see VintedClient.fetch_catalog_tree) and match each configured category's
`search` title within its gender department. Results are cached in
data/category_ids.json (committed) so we fetch the tree at most once per run and
survive transient changes. Resolution order per category mirrors the brands one:
cache hit -> resolve by name -> configured `id` fallback -> skip (logged).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import Category
from .client import VintedClient, _norm

log = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "category_ids.json"
_GENDER_TO_DEPT = {"men": "Men", "women": "Women"}


def _load_cache(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        return {k: int(v) for k, v in json.loads(path.read_text()).items()}
    except (ValueError, OSError):
        log.warning("Could not read category-id cache at %s; ignoring.", path)
        return {}


def _save_cache(path: Path, cache: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(sorted(cache.items())), indent=2) + "\n")


def _match(tree: list[tuple[int, str, str]], category: Category) -> int | None:
    dept = _GENDER_TO_DEPT[category.gender]
    target = _norm(category.search)
    in_dept = [(cid, _norm(title)) for cid, title, d in tree if d == dept]
    for cid, title in in_dept:            # exact (accent/case-folded) title first
        if title == target:
            return cid
    for cid, title in in_dept:            # then first close match
        if target and (target in title or title in target):
            return cid
    return None


def resolve_category_ids(
    client: VintedClient,
    categories: list[Category],
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> dict[str, int]:
    """Return {category_name: catalog_id} for every category we could resolve."""
    cache = _load_cache(cache_path)
    resolved: dict[str, int] = {}
    tree: list[tuple[int, str, str]] | None = None
    changed = False

    for category in categories:
        if category.name in cache:
            resolved[category.name] = cache[category.name]
            continue

        if tree is None:
            try:
                tree = client.fetch_catalog_tree()
                log.info("Fetched catalog tree (%d nodes).", len(tree))
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not fetch catalog tree: %s", exc)
                tree = []

        catalog_id = _match(tree, category)
        if catalog_id is None:
            catalog_id = category.id
            if catalog_id is not None:
                log.info("Using configured id %s for %s (resolution found nothing).",
                         catalog_id, category.name)
        else:
            log.info("Resolved %s (%s/%s) -> catalog %s",
                     category.name, category.gender, category.search, catalog_id)

        if catalog_id is None:
            log.warning("Could not resolve category '%s'; skipping it.", category.name)
            continue

        resolved[category.name] = catalog_id
        if cache.get(category.name) != catalog_id:
            cache[category.name] = catalog_id
            changed = True

    if changed:
        _save_cache(cache_path, cache)
        log.info("Updated category-id cache (%d categories).", len(cache))

    return resolved

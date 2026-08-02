"""One-shot diagnostic: capture condition (status) strings, size formats, and the
category tree so we can build quality + size + gender filters against real data.

Run in CI, read the logs, then delete this file.
"""
import os
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from src.config import load_config
from src.vinted.client import VintedClient

HEADERS = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}


def main() -> None:
    cfg = load_config()
    client = VintedClient(cfg)
    client.bootstrap()
    sess = client._ensure_session()
    base = cfg.base_url

    # Condition strings + size formats from men's outerwear (catalog 2052).
    r = sess.get(
        base + "/api/v2/catalog/items",
        params={"catalog_ids": 2052, "per_page": 96, "order": "newest_first", "currency": "GBP"},
        headers=HEADERS, timeout=30,
    )
    items = r.json().get("items") or []
    print("ITEM_KEYS:", sorted(items[0].keys()) if items else "none")
    print("STATUS_COUNTS:", dict(Counter(i.get("status") for i in items)))
    print("SAMPLE_SIZES:", [i.get("size_title") for i in items[:25]])

    # Does a category-tree endpoint exist? (for a category-by-name resolver)
    for path in ["/api/v2/catalogs", "/api/v2/catalog"]:
        try:
            rr = sess.get(base + path, headers=HEADERS, timeout=30)
            print(f"\n### {path} HTTP {rr.status_code}")
            print("BODY_HEAD:", rr.text[:900])
        except Exception as e:  # noqa: BLE001
            print(f"\n### {path} ERROR {e}")


if __name__ == "__main__":
    main()

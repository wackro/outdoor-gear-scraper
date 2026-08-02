"""One-shot diagnostic: dump Vinted response shapes so we can fix brand resolution.

Run in CI (which can reach Vinted), read the logs, then delete this file.
"""
import json
import os
import sys

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

    # 1) What does a catalog item actually contain? (brand id field?)
    r = sess.get(
        base + "/api/v2/catalog/items",
        params={"search_text": "Patagonia", "per_page": 3, "currency": "GBP"},
        headers=HEADERS, timeout=30,
    )
    print("### catalog/items HTTP", r.status_code)
    try:
        data = r.json()
        print("TOP_KEYS:", list(data.keys()))
        items = data.get("items") or []
        print("NUM_ITEMS:", len(items))
        if items:
            print("ITEM0_KEYS:", sorted(items[0].keys()))
            print("ITEM0_JSON:", json.dumps(items[0])[:2000])
    except Exception as e:
        print("catalog parse error:", e, "BODY_HEAD:", r.text[:400])

    # 2) Probe candidate brand-lookup endpoints.
    for path, params in [
        ("/api/v2/brands", {"keyword": "Patagonia"}),
        ("/api/v2/brands", {"search_text": "Patagonia"}),
        ("/api/v2/catalog/brands", {"search_text": "Patagonia"}),
        ("/api/v2/search_suggestions", {"query": "Patagonia"}),
    ]:
        try:
            rr = sess.get(base + path, params=params, headers=HEADERS, timeout=30)
            print(f"\n### {path} {params} -> HTTP {rr.status_code}")
            print("BODY_HEAD:", rr.text[:600])
        except Exception as e:
            print(f"\n### {path} {params} -> ERROR {e}")


if __name__ == "__main__":
    main()

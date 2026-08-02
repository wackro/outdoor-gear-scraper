"""Diagnostic v6: verify multi-brand_ids filtering + find bag category IDs."""
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from src.config import load_config
from src.vinted.client import VintedClient

H = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}
BAGS = re.compile(r"backpack|rucksack|\bbags?\b|holdall|duffel|duffle|daypack", re.I)
_NODE = re.compile(r'"id":(\d+),"title":"([^"]+)","url":"/catalog/')
DEPTS = ["Women", "Men", "Kids", "Home", "Electronics", "Entertainment", "Beauty", "Pet care"]


def main() -> None:
    cfg = load_config()
    client = VintedClient(cfg)
    client.bootstrap()
    sess = client._ensure_session()
    base = cfg.base_url

    # 1) Multi-brand filtering: comma form vs array form. TNF=2319, Patagonia=90804.
    for label, params in [
        ("COMMA", {"catalog_ids": 2052, "brand_ids": "2319,90804", "per_page": 60, "currency": "GBP"}),
    ]:
        items = sess.get(base + "/api/v2/catalog/items", params=params, headers=H, timeout=30).json().get("items") or []
        print(f"{label} n={len(items)} brand_titles={dict(Counter(i.get('brand_title') for i in items))}")
    # array form via repeated params
    arr = [("catalog_ids", 2052), ("brand_ids[]", 2319), ("brand_ids[]", 90804),
           ("per_page", 60), ("currency", "GBP")]
    items = sess.get(base + "/api/v2/catalog/items", params=arr, headers=H, timeout=30).json().get("items") or []
    print(f"ARRAY n={len(items)} brand_titles={dict(Counter(i.get('brand_title') for i in items))}")

    # 2) Bag categories by department from the homepage tree.
    html = sess.get(base + "/", timeout=30).text
    text = html.replace('\\"', '"').replace("\\/", "/").replace("\\u0026", "&")
    dept_off = {}
    for name in DEPTS:
        m = re.search(r'"title":"' + re.escape(name) + r'","url":"/catalog/', text)
        if m:
            dept_off[name] = m.start()
    def dept_for(off):
        c = [(o, n) for n, o in dept_off.items() if o <= off]
        return max(c)[1] if c else "?"
    print("--- bag categories ---")
    for m in _NODE.finditer(text):
        if BAGS.search(m.group(2)):
            d = dept_for(m.start())
            if d in ("Men", "Women"):
                print(f"{d:6} {int(m.group(1)):>6}  {m.group(2)}")


if __name__ == "__main__":
    main()

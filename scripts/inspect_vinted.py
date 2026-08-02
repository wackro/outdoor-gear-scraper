"""Diagnostic v3: locate Vinted's category tree so we can resolve categories by name.

The JSON catalog-tree API 404s, but the site server-renders the tree into the
homepage. Find it and show the structure around known nodes.
"""
import os
import re
import sys

sys.path.insert(0, os.getcwd())

from src.config import load_config
from src.vinted.client import VintedClient

HEADERS = {"Accept": "text/html"}


def main() -> None:
    cfg = load_config()
    client = VintedClient(cfg)
    client.bootstrap()
    sess = client._ensure_session()

    html = sess.get(cfg.base_url + "/", headers=HEADERS, timeout=30).text
    print("HTML_LEN", len(html))

    for kw in ['"catalogs"', '"catalog_tree"', '"code":"coats-and-jackets"',
               'Coats and jackets', 'Trousers', 'Trainers', 'Shoes', '"id":2052']:
        print(f"find {kw!r} -> {html.find(kw)}")

    # Show structure around the known men's coats node (id 2052) to reveal siblings.
    i = html.find('2052')
    if i >= 0:
        print("AROUND_2052:", html[max(0, i - 200):i + 400])

    # Pull any {"id":N,...,"title":"...","code":"..."} catalog-like objects.
    hits = re.findall(r'\{"id":\d+,[^{}]*?"title":"[^"]+","code":"[^"]+"[^{}]*?\}', html)
    print("CATALOG_OBJ_COUNT:", len(hits))
    for h in hits[:25]:
        print("OBJ:", h[:200])


if __name__ == "__main__":
    main()

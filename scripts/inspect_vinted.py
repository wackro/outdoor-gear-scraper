"""Diagnostic v4: extract garment category IDs (by gender) from the homepage tree.

The tree is embedded as escaped JSON: \"id\":N,\"title\":\"..\",\"url\":\"/catalog/N-slug\".
Parse it and print garment nodes bucketed by the nearest preceding gender root.
"""
import os
import re
import sys

sys.path.insert(0, os.getcwd())

from src.config import load_config
from src.vinted.client import VintedClient

GARMENT = re.compile(
    r"jacket|coat|gilet|body warmer|jumper|sweat|hoodie|fleece|cardigan|"
    r"trouser|jean|chino|short|shoe|trainer|boot|shirt|\btop\b|dress|skirt",
    re.I,
)
NODE = re.compile(r'\\"id\\":(\d+),\\"title\\":\\"(.*?)\\",\\"url\\":\\"/catalog/')


def main() -> None:
    cfg = load_config()
    client = VintedClient(cfg)
    client.bootstrap()
    html = client._ensure_session().get(cfg.base_url + "/", timeout=30).text

    roots = {}
    for g in ["Women", "Men", "Kids", "Home", "Electronics", "Entertainment", "Beauty", "Pet care"]:
        m = re.search(r'\\"title\\":\\"' + g + r'\\"', html)
        roots[g] = m.start() if m else -1
    print("ROOTS:", {k: v for k, v in roots.items() if v >= 0})

    nodes = [(int(m.group(1)), m.group(2), m.start()) for m in NODE.finditer(html)]
    print("TOTAL_NODES:", len(nodes))
    for nid, title, off in nodes:
        if not GARMENT.search(title):
            continue
        gender = max(
            ((o, name) for name, o in roots.items() if 0 <= o <= off),
            default=(-1, "?"),
        )[1]
        print(f"{gender:12} {nid:>7}  {title}")


if __name__ == "__main__":
    main()

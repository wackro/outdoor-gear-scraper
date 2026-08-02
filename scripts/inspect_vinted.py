"""Diagnostic v5: extract Men's/Women's garment category IDs from the homepage tree.

Unescape the embedded JSON, locate department nodes ("title":"Men","url":"/catalog/..."),
and bucket garment category nodes by the nearest preceding department node.
"""
import os
import re
import sys

sys.path.insert(0, os.getcwd())

from src.config import load_config
from src.vinted.client import VintedClient

WANT = re.compile(
    r"coat|jacket|gilet|body warmer|jumper|hoodie|sweat|fleece|cardigan|"
    r"\btops?\b|t-shirt|shirt|trouser|jean|chino|shoe|trainer|boot",
    re.I,
)
NODE = re.compile(r'"id":(\d+),"title":"([^"]+)","url":"/catalog/')
DEPTS = ["Women", "Men", "Kids", "Home", "Electronics", "Entertainment", "Beauty", "Pet care"]


def main() -> None:
    cfg = load_config()
    client = VintedClient(cfg)
    client.bootstrap()
    html = client._ensure_session().get(cfg.base_url + "/", timeout=30).text
    u = html.replace('\\"', '"').replace("\\/", "/").replace("\\u0026", "&")

    dept_offsets = {}
    for name in DEPTS:
        m = re.search(r'"title":"' + re.escape(name) + r'","url":"/catalog/', u)
        dept_offsets[name] = m.start() if m else -1
    print("DEPT_OFFSETS:", {k: v for k, v in dept_offsets.items() if v >= 0})

    def dept_for(off: int) -> str:
        cands = [(o, n) for n, o in dept_offsets.items() if 0 <= o <= off]
        return max(cands)[1] if cands else "?"

    seen = set()
    for m in NODE.finditer(u):
        nid, title = int(m.group(1)), m.group(2)
        if not WANT.search(title) or (nid, m.start() // 100000) in seen:
            continue
        d = dept_for(m.start())
        if d in ("Men", "Women"):
            print(f"{d:6} {nid:>6}  {title}")


if __name__ == "__main__":
    main()

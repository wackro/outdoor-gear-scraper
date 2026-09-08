"""Dump Vinted's catalog tree, so categories can be chosen rather than guessed.

    python -m scripts.list_categories [department ...]

Vinted has no catalog-tree API; `VintedClient.fetch_catalog_tree()` recovers it
from the JSON the homepage server-renders. This prints every node it finds,
grouped by department, marking the ones already in config so it is obvious
what is and isn't covered.

Writes to $GITHUB_STEP_SUMMARY as well as stdout, so the result is readable on
a phone from the workflow's job page.
"""
from __future__ import annotations

import logging
import os
import sys

from src.config import load_config
from src.vinted.client import VintedClient

log = logging.getLogger("catalog")


def render(nodes: list[tuple[int, str, str]], configured: set[int],
           departments: list[str]) -> str:
    by_dept: dict[str, list[tuple[int, str]]] = {}
    for node_id, title, dept in nodes:
        if departments and dept not in departments:
            continue
        by_dept.setdefault(dept or "(untagged)", []).append((node_id, title))

    out: list[str] = ["## Vinted catalog tree", ""]
    for dept in sorted(by_dept):
        entries = sorted(set(by_dept[dept]), key=lambda e: e[1].lower())
        out.append(f"### {dept} — {len(entries)} categories")
        out.append("")
        out.append("| id | title | in config |")
        out.append("| --- | --- | --- |")
        for node_id, title in entries:
            mark = "**yes**" if node_id in configured else ""
            out.append(f"| `{node_id}` | {title} | {mark} |")
        out.append("")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    departments = argv[1:] or ["Men"]

    config = load_config()
    configured = {c.id for c in config.categories if c.id}

    client = VintedClient(config)
    nodes = client.fetch_catalog_tree()
    if not nodes:
        print("No catalog nodes found — the homepage layout may have changed.")
        return 1
    log.info("Found %d nodes across all departments.", len(nodes))

    report = render(nodes, configured, departments)
    print(report)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

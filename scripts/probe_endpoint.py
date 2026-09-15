"""Find out why the Vinted catalog endpoint is refusing us.

    python -m scripts.probe_endpoint

Written for the case where the scrape has started failing and nobody can tell
whether Vinted moved the endpoint, started rejecting one of our parameters, or
began blocking us behind a 404. It sends the real requests, reports exactly what
came back, and says which of those three it looks like.

Output goes to $GITHUB_STEP_SUMMARY as well as stdout, so it is readable from a
phone on the job page. Read-only: it touches no database, publishes nothing, and
exits 0 even when every request fails -- a failing probe is a successful
diagnosis.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config                                    # noqa: E402
from src.vinted.brand_resolver import resolve_brands                  # noqa: E402
from src.vinted.client import BRANDS_PATH, CATALOG_PATH, VintedClient  # noqa: E402
from src.vinted.diagnose import (                                     # noqa: E402
    Probe, diagnose, hunt_for_replacement, interpret, summarise_hunt,
)

log = logging.getLogger("probe")


def render(probes: list[Probe], headline: str, *,
           hunt: list[Probe] | None = None, hunt_headline: str = "",
           notes: list[str] | None = None) -> str:
    """Markdown, because the job summary renders it and a phone has to read it."""
    lines = ["# Vinted endpoint probe", ""]

    # The hunt goes first when there is one: if a replacement was found, that is
    # the only thing worth reading, and the diagnosis below is just evidence.
    if hunt:
        lines += [hunt_headline, "", "## Candidate endpoints", "",
                  "| path | result | where it came from |", "| --- | --- | --- |"]
        for probe in hunt:
            mark = "**USABLE**" if probe.usable else ("OK" if probe.ok else "FAIL")
            lines.append(f"| `{probe.name}` | {mark} — {probe.verdict} | {probe.asks} |")

        # Coverage decides whether a working endpoint is actually a fix. A field
        # present on a couple of listings and absent on the rest reads as success
        # in a sample and starves the signal in production.
        covered = [p for p in hunt if p.coverage]
        if covered:
            lines += ["", "### Field coverage of what answered", "",
                      "% of returned listings carrying each field the scraper reads.", ""]
            fields = list(covered[0].coverage)
            lines.append("| endpoint | " + " | ".join(f"`{f}`" for f in fields) + " |")
            lines.append("| --- |" + " --- |" * len(fields))
            for probe in covered:
                cells = " | ".join(f"{probe.coverage[f]:.0f}%" for f in fields)
                lines.append(f"| `{probe.name}` | {cells} |")
        if notes:
            lines += ["", "<details><summary>How the paths were found</summary>", ""]
            lines += [f"- {note}" for note in notes]
            lines += ["", "</details>", ""]
        lines += ["", "## Diagnosis", ""]

    lines += [headline, "",
              "| request | result | asks |", "| --- | --- | --- |"]
    for probe in probes:
        mark = "OK" if probe.ok else "**FAIL**"
        lines.append(f"| `{probe.name}` | {mark} — {probe.verdict} | {probe.asks} |")

    lines += ["", "## What each request actually returned", ""]
    for probe in probes:
        lines += [f"### {probe.name}", "", f"- **{probe.verdict}**", f"- `{probe.url}`"]
        if probe.headers:
            shown = ", ".join(f"`{k}: {v}`" for k, v in sorted(probe.headers.items()))
            lines.append(f"- headers: {shown}")
        if probe.body:
            lines += ["", "```", probe.body, "```"]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config()
    client = VintedClient(config)

    # Brand resolution is itself a signal: it uses /api/v2/brands, so if it works
    # the session and our IP are fine and only the catalog path is in question.
    try:
        brands = resolve_brands(client, config.brands)
        brand_ids = list(brands.ids.values())
        log.info("Resolved %d brand ids.", len(brand_ids))
    except Exception as exc:  # noqa: BLE001 -- the probe must still run
        log.warning("Brand resolution failed (%s); probing with a known id.", exc)
        brand_ids = [2319]      # The North Face, stable and long-lived

    catalog_id = config.categories[0].id if config.categories else 2052
    probes = diagnose(
        client,
        catalog_id=catalog_id,
        brand_ids=brand_ids,
        catalog_path=CATALOG_PATH,
        brands_path=BRANDS_PATH,
    )

    hunt, notes = hunt_for_replacement(client, catalog_id=catalog_id, brand_ids=brand_ids)

    report = render(probes, interpret(probes),
                    hunt=hunt, hunt_headline=summarise_hunt(hunt), notes=notes)
    print(report)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report + "\n")

    # Always 0. The probe succeeded if it produced a report, whatever it says.
    return 0


if __name__ == "__main__":
    sys.exit(main())

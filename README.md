# Outdoor Gear Deals — Vinted UK bargain scraper

A small, self-contained website that highlights **bargains on outdoor clothing
spotted on Vinted UK**. Every day it scrapes the newest Vinted listings for a
customizable set of outdoor brands, works out whether each listed price is a
genuine bargain (by comparing it to the recent price history for that brand and
category), and publishes the good ones to a static website.

No servers, no database service, no running costs: a scheduled **GitHub Action**
does the scraping, a **SQLite file in the repo** stores the history, and the site
is served from **GitHub Pages**.

## Features

- **Customizable brand watchlist** — pick which outdoor brands to track in
  `config/config.yaml`. Seeded with Arc'teryx, Patagonia, Rab, Montane, The North
  Face, Fjällräven, Mammut, Berghaus and Haglöfs.
- **Daily automated scrape** of the newest Vinted UK listings for those brands.
- **Bargain detection** — flags items priced well below the running median for
  their brand + category, with a configurable discount threshold (default 30%)
  and optional per-brand overrides.
- **Deals website** — a card grid showing photo, brand, price vs. baseline,
  discount %, size and a link straight to the Vinted listing. Sort and filter by
  discount, deal score, brand, category or recency, all client-side.
- **Price-history tracking** — every observed price is recorded, so the baseline
  gets more accurate over time.
- **Hybrid & RRP-ready** — uses price history by default and falls back to
  configured retail prices (RRP) where available; the baseline layer is a clean
  interface, so richer pricing sources can be added later.

## How it works

```
scrape (Vinted API) → store items + price observations (SQLite)
   → recompute per-brand/category median baseline
   → flag items priced ≥ threshold below baseline
   → render static site to docs/  → GitHub Pages
```

The daily [`daily-vinted-deals`](.github/workflows/daily.yml) workflow runs the
whole pipeline (`python -m src.run`), commits the updated `data/vinted.db` and
`docs/`, and Pages serves the result.

## Setup

1. **Fork / use this repo.**
2. **Verify brand and category IDs** in `config/config.yaml` (see below) — the
   seeded IDs are placeholders and must be checked against the live site.
3. **Enable GitHub Pages:** repo *Settings → Pages → Build and deployment →
   Deploy from a branch*, and choose your default branch with the **`/docs`**
   folder. The daily commit then publishes automatically.
4. That's it. The workflow runs daily and can also be triggered manually from the
   *Actions* tab (**Run workflow**).

> **Day one shows no deals — this is expected.** Deals are judged against each
> brand's *own* recent price history, which needs a week or two of daily scraping
> to build up. To surface deals sooner, add `rrp` values to brands in the config
> (see below).

## Configuration

Everything lives in `config/config.yaml` (copy from `config/config.example.yaml`).

Key knobs under `deals`:

| Setting | Meaning | Default |
| --- | --- | --- |
| `threshold` | Flag when `price ≤ median × (1 − threshold)` | `0.30` (30% off) |
| `min_samples` | Observations needed before a bracket can flag deals | `8` |
| `window_days` | How far back price history feeds the baseline | `90` |
| `stale_days` | Items unseen this long are marked sold/inactive | `5` |

### Adding or verifying brands

Vinted has no "list all brands" endpoint, so each brand's numeric id is read once
from the live site:

1. Go to [vinted.co.uk](https://www.vinted.co.uk) and search for the brand (and,
   ideally, a category).
2. Look at the URL — it contains `brand_ids[]=NNNN` (the brand id) and
   `catalog[]=NNNN` (the category id).
3. Add them to `config.yaml`:

```yaml
brands:
  patagonia:
    id: 6392
    threshold: 0.35        # optional per-brand override
    rrp:                   # optional retail prices, used as a fallback baseline
      mens_outerwear: 250

categories:
  mens_outerwear: 2052
```

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Smoke-test the scraper against one brand:
python -m src.vinted.client

# Full pipeline (scrape → store → detect → render):
python -m src.run

# Then open docs/index.html in a browser.
```

## Project structure

```
config/            config.yaml, config.example.yaml
src/vinted/        client.py (session + fetch), models.py
src/storage/       schema.sql, db.py
src/pricing/       baseline.py, deals.py
src/site/          generator.py, templates/, static/
src/config.py      config loader
src/run.py         orchestrator
data/vinted.db     committed SQLite (source of truth)
docs/              generated site (GitHub Pages source)
.github/workflows/daily.yml
```

## Caveats & legal

This uses Vinted's **undocumented internal API**. Please be a good citizen:

- Vinted's Terms prohibit automated access — keep this personal and low-volume
  (the defaults are deliberately gentle: a few pages per brand, with delays).
- Don't rehost or resell the scraped data commercially; the site links back to
  the original listings and carries a "not affiliated / may be inaccurate"
  disclaimer.
- Don't store sellers' personal data.
- The endpoint can change or start blocking at any time (Vinted uses DataDome
  anti-bot; this project uses `curl_cffi` browser impersonation to cope, and
  keeps the last-good database if a scrape is blocked). Expect occasional
  maintenance. If GitHub's shared IPs get blocked, set a `PROXY_URL` secret to a
  residential proxy.

Not affiliated with Vinted.

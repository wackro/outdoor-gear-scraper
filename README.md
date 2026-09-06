# Outdoor Gear Deals — Vinted UK bargain sniper

Watches **Vinted UK for bargains on outdoor clothing** and pushes them to your
phone within minutes of listing.

Good listings sell in under an hour, so the app runs two paths at different
speeds:

- **The hot path** polls the newest listings every ~60 seconds and pushes an
  instant notification when one starts *heating up* — gaining views and
  favourites unusually fast. That acceleration is the market valuing the item for
  you, and it is a far better bargain detector than price alone.
- **The cold path** still runs once a day: it scrapes for price history, works
  out per-brand baselines, and publishes everything it finds to a static site for
  browsing at leisure.

No servers, no database service, no running costs: **GitHub Actions** does the
work, a **SQLite file in the repo** stores the history, and the site is served
from **GitHub Pages**.

## Instant alerts

The alert path asks a different question from the website. The site asks *"is
this cheap relative to its brand's median?"*, which on thin data produces noise —
a £1 backpack strap looks like 97% off a £33 backpack. The alerts instead ask
*"are people piling onto this right now?"*

- **Hotness is the trigger.** Every tracked listing's favourite and view counts
  are sampled repeatedly, and the alert fires on the *rate of gain*, not the raw
  count. Views accrue before favourites, so they detect earlier; favourites are a
  stronger statement of intent, so they weigh more.
- **The bar adapts.** It is the higher of an absolute floor and the 99th
  percentile of what listings are currently achieving, so alert volume stays
  roughly steady without seasonal retuning. The floor is what stops it alerting
  on the least-cold listing in a dead market.
- **Price is only a veto.** A hot listing at full price is not a bargain and is
  dropped. Crucially, a listing with *no* price baseline still alerts — which is
  how rare brands, the ones with too little history to ever be flagged by price,
  finally get caught.
- **It won't spam you.** Every listing alerts at most once, dedup survives losing
  the poller's cache, and a hard hourly burst cap means a bug costs you a handful
  of notifications rather than five hundred.

Alerts go to [ntfy.sh](https://ntfy.sh) — free, no account needed, and the push
carries the photo, price, size, condition and like-rate with a one-tap link
straight to the listing.

> **Known trade-off:** hotness is a *lagging* signal. A listing has to attract
> attention before it can look hot, and whoever generated that attention got
> there first. This buys high-confidence alerts on listings that are provably
> moving, not first-mover advantage. Polling fast and measuring over a listing's
> first minutes keeps the lag as short as it can be.

### Setting up alerts

1. Install the ntfy app ([iOS](https://apps.apple.com/us/app/ntfy/id1625396347) /
   [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)).
2. Pick a topic name and set it as `alerts.ntfy_topic` in `config/config.yaml`.
   **Make it unguessable** — topics on the public server are readable by anyone
   who knows the name.
3. Subscribe to that topic in the app.
4. Optionally add an `NTFY_TOKEN` repo secret. Worth doing: it moves rate
   limiting from the shared GitHub runner IP to your own account.
5. Enable the `vinted-hot-poller` workflow.

Try it without pushing anything first:

```bash
python -m src.hot.poller --dry-run --once   # one cycle, printed to the terminal
python -m src.hot.poller --dry-run          # shadow-run and watch what it'd send
```

Tune `alerts.floor_favourites_per_hour` and `alerts.min_favourites_gain` from
what you see. **Treat the shipped defaults as guesses** — they have not been
calibrated against real traffic.

## Features

- **Customizable brand watchlist** — pick which outdoor brands to track in
  `config/config.yaml`. Seeded with Arc'teryx, Patagonia, Rab, Montane, The North
  Face, Fjällräven, Mammut, Berghaus and Haglöfs.
- **Daily automated scrape** of the newest Vinted UK listings for those brands.
- **Bargain detection** — flags items priced well below the running median for
  their brand + category, with a configurable discount threshold (default 30%)
  and optional per-brand overrides.
- **Quality floor** — each item's condition is shown, and anything below a
  configurable floor (default **Good**) is hidden.
- **Size filtering** — strict, garment-type-aware allow-lists so only sizes you
  can actually wear show up (e.g. men's shoes UK 8.5/9, clothes M/L, trousers
  30/32 waist; women's have their own set).
- **Men's & Women's sections** — the site opens on Men's by default, with a
  Women's tab that uses its own size rules.
- **Deals website** — a card grid showing photo, brand, condition, price vs.
  baseline, discount %, size and a link to the Vinted listing. Sort and filter by
  discount, deal score, brand, type or recency, all client-side.
- **Price-history tracking** — every observed price is recorded, so the baseline
  gets more accurate over time.
- **Hybrid & RRP-ready** — uses price history by default and falls back to
  configured retail prices (RRP) where available.

## How it works

```
HOT PATH — every ~60s, notifies, commits nothing
  poll newest listings → sample favourites/views → measure rate of gain
     → compare against the adaptive bar → price veto → push to your phone
                                              ↑
                                     baselines table (read-only)
                                              ↑
COLD PATH — once a day, owns the data and the site
  scrape (Vinted API) → store items + price observations (SQLite)
     → recompute per-brand/category median baseline
     → flag items priced ≥ threshold below baseline
     → render static site to docs/ → GitHub Pages → commit
```

The [`vinted-hot-poller`](.github/workflows/poller.yml) workflow runs the fast
loop; [`daily-vinted-deals`](.github/workflows/daily.yml) runs the daily pipeline
(`python -m src.run`) and commits `data/vinted.db` and `docs/`.

**Why a long-running job rather than a frequent cron.** GitHub's scheduled runs on
this repo land four to seven hours after their cron time — a `*/5` schedule would
inherit exactly that unreliability. So cron is used only to *start* a job, which
then does its own precise timing internally for 5h45m. A new run cancels the
incumbent, so scheduling delay shifts the handover rather than leaving a gap.

**Why the poller never commits.** `data/vinted.db` is tens of megabytes and
committed to git. Writing it every 60 seconds is impossible to commit and would
bloat the repo without bound. The poller keeps a small, disposable state file in
the Actions cache instead; the daily job folds its alert log into the committed
DB so dedup survives the cache being evicted.

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

### Adding brands

Just list the brand by name — its Vinted brand id is **resolved automatically**
at scrape time and cached in `data/brand_ids.json`, so you don't need to hunt for
numeric ids:

```yaml
brands:
  patagonia:                 # resolved by name
  arcteryx:
    search: "Arc'teryx"      # only needed if the display name differs from the key
    threshold: 0.35          # optional per-brand override
    rrp:                     # optional retail prices, used as a fallback baseline
      mens_outerwear: 250
  the_north_face:
    id: 2319                 # optional: pin a known id (skips resolution / used as fallback)

categories:
  mens_outerwear: 2052       # category ids are still read from a Vinted URL (see below)
```

If a name can't be resolved, the run logs it and skips that brand (falling back to
`id` if you provided one). To pin an id yourself, read `brand_ids[]=NNNN` from a
brand search URL on [vinted.co.uk](https://www.vinted.co.uk).

### Categories, sizes and quality

**Categories** are configured by name too — each entry has a `gender`
(`men`/`women`), a garment `type` (`clothes`/`trousers`/`shoes`) and a `search`
title that's resolved to a Vinted catalog id (from the site's category tree,
cached in `data/category_ids.json`; the `id` field is a fallback):

```yaml
categories:
  - {gender: men,   type: clothes,  search: "Jackets"}
  - {gender: women, type: shoes,    search: "Shoes"}
```

**Sizes** are an allow-list per gender + type; anything else is hidden. Matching
is strict and type-aware (shoes read the UK number, trousers read the waist,
clothes read the letter/number size):

```yaml
sizes:
  men:   {clothes: [M, L],        trousers: [30, 32], shoes: [8.5, 9]}
  women: {clothes: [XS, S, 6, 8], trousers: [8, 10],  shoes: [5]}
```

**Quality** hides anything below the floor (New with tags > New without tags >
Very good > Good > Satisfactory):

```yaml
quality:
  floor: Good
```

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Check the API still returns what the poller depends on. Run this first —
# everything else assumes it passes.
python -m scripts.probe_api

# Tests (no network, no fixtures):
python -m pytest tests/ -q

# Smoke-test the scraper against one brand:
python -m src.vinted.client

# Fast poller, printing alerts instead of pushing them:
python -m src.hot.poller --dry-run --once

# Full daily pipeline (scrape → store → detect → render):
python -m src.run

# Then open docs/index.html in a browser.
```

## Project structure

```
config/            config.yaml, config.example.yaml
src/vinted/        client.py (session + fetch), models.py
src/storage/       schema.sql, db.py
src/pricing/       baseline.py, deals.py
src/hot/           hotness.py (velocity maths), thresholds.py (the adaptive bar),
                   alerts.py (rules), state.py (cache-backed state), poller.py (loop)
src/notify/        base.py (interface + burst cap), ntfy.py, console.py
src/site/          generator.py, templates/, static/
src/config.py      config loader
src/run.py         daily orchestrator
scripts/probe_api.py   verifies the API still returns what the poller needs
tests/             pytest suite (no network required)
data/vinted.db     committed SQLite (source of truth)
data/hot.db        poller working state — gitignored, lives in the Actions cache
docs/              generated site (GitHub Pages source)
.github/workflows/ daily.yml, poller.yml, tests.yml
```

## Caveats & legal

This uses Vinted's **undocumented internal API**. Please be a good citizen:

- Vinted's Terms prohibit automated access — keep this personal and low-volume.
  The fast poller sweeps a rotating slice of categories rather than all of them
  each cycle, which holds it to roughly two requests a minute. It jitters its
  interval, refreshes its session periodically, backs off overnight, and on a 429
  or a DataDome challenge enters an escalating cooldown rather than retrying —
  hammering a soft block is how it becomes a lasting one.
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

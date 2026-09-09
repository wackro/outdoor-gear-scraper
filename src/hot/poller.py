"""The fast poller: watch the newest listings and push the hot ones instantly.

Run as `python -m src.hot.poller`. It loops until told to stop, so it is designed
to live inside a long-running GitHub Actions job rather than to be re-invoked by
cron — GitHub's scheduled runs on this repo land four to seven hours late, which
is useless for something measured in minutes.

Why a rotating per-category sweep rather than one big query: the catalog response
does not reliably say which category an item came from, and we need that to know
its gender and garment type — without which the size allow-list cannot be applied
and every alert would be for something unwearable. Asking per category means the
answer comes back with the question.
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config, load_config
from ..notify import Notifier, build_notifier
from ..storage.db import DEFAULT_DB_PATH
from ..vinted.brand_resolver import BrandResolution, resolve_brands
from ..vinted.client import VintedBlocked, VintedClient, VintedError, _norm
from .alerts import BaselineLookup, evaluate, population_rates
from .feed import build_feed, feed_changed
from .publish import publish_feed
from .sold import detect_gone
from .state import DEFAULT_HOT_DB, HotState
from .thresholds import Bar, build_bar

log = logging.getLogger("poller")


@dataclass
class Stats:
    """What the run did, for the end-of-job summary."""
    started: float = field(default_factory=time.time)
    ticks: int = 0
    requests: int = 0
    items_seen: int = 0
    items_tracked: int = 0
    # Whether Vinted actually returns the counter the whole hot path depends on.
    # Surfaced in the run summary because it is the one assumption that cannot be
    # checked without hitting the live API.
    items_with_likes: int = 0
    alerts_sent: int = 0
    sold_seen: int = 0
    feeds_published: int = 0
    blocks: int = 0
    errors: int = 0

    def as_markdown(self, bar: Bar, *, channel: str = "") -> str:
        mins = (time.time() - self.started) / 60
        if self.items_tracked == 0:
            likes = "no listings tracked yet"
        elif self.items_with_likes == 0:
            likes = (f"**0 of {self.items_tracked}** — `favourite_count` is absent "
                     f"from the API response, so nothing can ever look hot")
        else:
            likes = f"{self.items_with_likes} of {self.items_tracked}"
        lines = [
            "## Vinted poller run",
            "",
            f"| Metric | Value |",
            f"| --- | --- |",
            f"| Runtime | {mins:.0f} min |",
            f"| Poll cycles | {self.ticks} |",
            f"| Requests | {self.requests} |",
            f"| Listings seen | {self.items_seen} |",
            f"| Listings tracked | {self.items_tracked} |",
            f"| Listings with like counts | {likes} |",
            f"| **Alerts sent** | **{self.alerts_sent}** |",
            f"| Listings sold while watched | {self.sold_seen} |",
            f"| Feed publishes | {self.feeds_published} |",
            f"| Blocked/rate-limited | {self.blocks} |",
            f"| Errors | {self.errors} |",
            f"| Alert bar | {bar.fav_cut * 60:.1f} likes/hr "
            f"({'adaptive' if bar.adaptive else 'floor'}, n={bar.sample_size}) |",
        ]
        if channel == "console":
            # Otherwise a run that finds things but sends nothing reads as a
            # broken notifier rather than a deliberate setting.
            lines += [
                "",
                "> Alerts are going to this log, not to a phone "
                "(`alerts.channel: console`). Set `alerts.ntfy_topic` and switch "
                "`channel` to `ntfy` to receive them.",
            ]
        return "\n".join(lines)


class Poller:
    def __init__(
        self,
        config: Config,
        state: HotState,
        client: VintedClient,
        notifier: Notifier,
        brands: BrandResolution,
        *,
        dry_run: bool = True,
    ):
        # Defaults to True on purpose. Publishing the feed force-pushes a git
        # branch, so the unsafe direction has to be the one you opt into: any
        # caller that forgets to decide -- a test, a REPL, a future entry point --
        # gets the harmless behaviour rather than pushing to a real remote.
        self.config = config
        self.state = state
        self.client = client
        self.notifier = notifier
        self.brands = brands
        self.brand_ids = list(brands.ids.values())
        self.categories = list(config.categories)
        self.baselines = BaselineLookup({})

        self.bar = build_bar(
            [], [],
            pct=config.alerts.percentile,
            floor_per_hour=config.alerts.floor_favourites_per_hour,
            min_population=config.alerts.min_population,
        )
        self.stats = Stats()
        self._stop = False
        self._rotation = 0
        self._consecutive_failures = 0
        self._cooldown = 0.0
        self._last_session_refresh = time.time()
        self._last_bar_refresh = 0.0
        self._last_prune = time.time()
        self._last_feed_publish = 0.0
        self._last_feed: dict | None = None
        self.repo_dir = Path(__file__).resolve().parent.parent.parent
        self.dry_run = dry_run

    # -- lifecycle ----------------------------------------------------------

    def request_stop(self, signum, _frame) -> None:
        """Stop at the end of the current cycle rather than mid-write.

        Actions sends SIGTERM before killing a job at the 6-hour limit; finishing
        the cycle means the state file we hand to the cache is consistent.
        """
        log.info("Signal %s received — finishing current cycle then exiting.", signum)
        self._stop = True

    def load_baselines(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        """Read the price baselines the daily job materialised.

        Read-only and best-effort: the poller must start even if the DB is
        missing, since the price check is only a veto on top of hotness.
        """
        import sqlite3
        if not Path(db_path).exists():
            log.warning("No %s — running without the price sanity check.", db_path)
            return
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            self.baselines = BaselineLookup.from_db(conn, self.config.deals.min_samples)
            self._seed_dedup(conn)
            conn.close()
            log.info("Loaded %d baselines.", len(self.baselines))
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read baselines from %s: %s", db_path, exc)

    def _seed_dedup(self, conn) -> None:
        """Carry forward what we already alerted on, in case the cache was lost.

        Without this a cache miss would re-push every still-live listing we
        already told the user about — the surest way to get the alerts muted.
        """
        try:
            rows = conn.execute("SELECT item_id FROM alerted").fetchall()
        except Exception:  # noqa: BLE001 — table won't exist until the daily job adds it
            return
        seeded = self.state.seed_alerted([r[0] for r in rows])
        if seeded:
            log.info("Seeded %d previously-alerted items into dedup.", seeded)

    # -- pacing -------------------------------------------------------------

    def _interval(self) -> float:
        """How long to wait before the next cycle.

        Jittered so the request pattern isn't a metronome, and stretched
        overnight when there is almost nothing new to find.
        """
        poll = self.config.poll
        interval = poll.interval_sec
        hour = datetime.now(timezone.utc).hour
        night = poll.night
        in_night = (
            night.start_hour <= hour < night.end_hour
            if night.start_hour <= night.end_hour
            else hour >= night.start_hour or hour < night.end_hour
        )
        if in_night:
            interval *= night.multiplier
        return interval * (1 + random.uniform(-poll.jitter, poll.jitter))

    def _next_categories(self) -> list:
        """The slice of categories to sweep this cycle.

        Round-robin rather than all-at-once: a full sweep every cycle would be 14
        requests a minute against a DataDome-protected endpoint. Spreading them
        keeps us near two requests a minute while still revisiting every category
        every few minutes — which is exactly the cadence a favourite-count delta
        needs anyway.
        """
        per_tick = max(1, self.config.poll.deep_pages)
        total = len(self.categories)
        if total == 0:
            return []
        picked = [
            self.categories[(self._rotation + i) % total]
            for i in range(min(per_tick, total))
        ]
        self._rotation = (self._rotation + len(picked)) % total
        return picked

    # -- the work -----------------------------------------------------------

    def _sweep(self, categories: list) -> None:
        """Fetch one page for each category and record what came back."""
        now = time.time()
        for category in categories:
            if self._stop:
                return
            catalog_id = category.id
            try:
                items = self.client.fetch_page(self.brand_ids, catalog_id, page=1)
                self.stats.requests += 1
            except VintedBlocked as exc:
                # Rate-limited or challenged: back off hard rather than retry.
                self.stats.errors += 1
                self.stats.blocks += 1
                self._consecutive_failures += 1
                log.warning("Blocked fetching %s: %s", category.label, exc)
                self._enter_cooldown()
                return
            except VintedError as exc:
                self.stats.errors += 1
                self._consecutive_failures += 1
                log.warning("Fetch failed for %s: %s", category.label, exc)
                return
            except Exception as exc:  # noqa: BLE001 — never die mid-loop
                self.stats.errors += 1
                self._consecutive_failures += 1
                log.warning("Unexpected fetch error for %s: %s", category.label, exc)
                return

            self._consecutive_failures = 0
            self.stats.items_seen += len(items)
            for item in items:
                brand = self.brands.name_by_title.get(_norm(item.brand_title))
                if brand is None:
                    continue  # not on the watchlist
                self.state.record(
                    item, brand=brand, catalog_id=catalog_id,
                    gender=category.gender, garment_type=category.type, now=now,
                )
                self.stats.items_tracked += 1
                if item.favourite_count is not None:
                    self.stats.items_with_likes += 1

            self._detect_sales(category, items, now)
            self.client.throttle()
        self.state.commit()

    def _detect_sales(self, category, items: list, now: float) -> None:
        """Mark listings that should have been on this page but weren't.

        Only meaningful because we poll fast enough for absence to mean something.
        See `src/hot/sold.py` for why this is not simply "wasn't in the response".
        """
        tracked = self.state.tracked_in_category(
            category.id, now - self.config.alerts.max_age_minutes * 60
        )
        if not tracked:
            return
        gone = detect_gone(
            tracked,
            {item.id for item in items},
            [item.listed_ts for item in items if item.listed_ts],
        )
        marked = self.state.mark_gone(gone, now)
        if marked:
            self.stats.sold_seen += marked
            log.info("%s: %d listing(s) gone (likely sold).", category.label, marked)

    def _enter_cooldown(self) -> None:
        """Back off hard after a block, doubling each time.

        Hammering a DataDome challenge is how a soft rate-limit becomes a lasting
        IP ban, which would take the whole system down rather than one cycle.
        """
        poll = self.config.poll
        self._cooldown = min(
            poll.cooldown_max_sec,
            max(poll.cooldown_base_sec, self._cooldown * 2 or poll.cooldown_base_sec),
        )
        log.warning("Blocked — cooling down for %.0fs.", self._cooldown)

    def _refresh_bar(self, now: float) -> None:
        """Recompute the adaptive alerting bar from recent velocities."""
        fav_rates, view_rates = population_rates(self.state, self.config, now=now)
        self.bar = build_bar(
            fav_rates, view_rates,
            pct=self.config.alerts.percentile,
            floor_per_hour=self.config.alerts.floor_favourites_per_hour,
            min_population=self.config.alerts.min_population,
        )
        self._last_bar_refresh = now
        log.info(
            "Bar: %.2f likes/hr (%s, n=%d)",
            self.bar.fav_cut * 60, "adaptive" if self.bar.adaptive else "floor",
            self.bar.sample_size,
        )

    def _dispatch(self, now: float) -> None:
        """Score everything tracked and push whatever clears the bar."""
        alerts = evaluate(self.state, self.config, self.bar, self.baselines, now=now)
        for alert in alerts:
            if self.notifier.send(alert):
                self.stats.alerts_sent += 1
            # Marked either way: a failed push is not a reason to retry forever,
            # and the burst cap deliberately drops alerts it does not want sent.
            self.state.mark_alerted(
                alert.item_id, heat=alert.heat,
                fav_rate=alert.fav_per_hour, view_rate=alert.view_per_hour,
                price=alert.price, baseline=alert.baseline,
            )
        if alerts:
            self.state.commit()

    def tick(self) -> None:
        """One full cycle: sweep, rescore, alert, housekeep."""
        now = time.time()
        self.stats.ticks += 1

        if time.time() - self._last_session_refresh > self.config.poll.session_refresh_min * 60:
            log.info("Refreshing Vinted session.")
            self.client.reset_session()
            self._last_session_refresh = time.time()

        self._sweep(self._next_categories())

        if now - self._last_bar_refresh > self.config.poll.threshold_refresh_sec:
            self._refresh_bar(now)

        self._dispatch(now)
        self._publish_feed(now)

        if now - self._last_prune > 1800:
            samples, alerts = self.state.prune(now=now)
            self._last_prune = now
            log.debug("Pruned %d samples, %d stale alerts.", samples, alerts)

    def _publish_feed(self, now: float) -> None:
        """Refresh the website's data, if anything worth showing has changed.

        Debounced and change-gated: the rates wobble slightly every cycle, and
        without both guards this would push a commit every single minute.
        Failures are swallowed — losing a site refresh must never stop the
        polling, which is the part that actually notifies you.
        """
        poll = self.config.poll
        if not poll.feed_enabled:
            return
        if now - self._last_feed_publish < poll.feed_publish_interval_sec:
            return
        try:
            feed = build_feed(self.state, self.config, self.bar, self.baselines,
                              now=now, limit=poll.feed_limit)
            if not feed_changed(self._last_feed, feed):
                self._last_feed_publish = now
                return
            if publish_feed(feed, branch=poll.feed_branch, repo_dir=self.repo_dir,
                            dry_run=self.dry_run):
                self.stats.feeds_published += 1
            self._last_feed = feed
            self._last_feed_publish = now
        except Exception:  # noqa: BLE001 — the loop outranks the website
            log.exception("Feed publish failed")

    def run(self, *, once: bool = False, max_runtime: float | None = None) -> int:
        deadline = time.time() + (max_runtime or self.config.poll.max_runtime_sec)
        log.info(
            "Polling %d categories across %d brands; %d per cycle, ~%.0fs apart.",
            len(self.categories), len(self.brand_ids),
            max(1, self.config.poll.deep_pages), self.config.poll.interval_sec,
        )

        # Prime the bar so the first cycle isn't scored against nothing.
        self._refresh_bar(time.time())

        while not self._stop:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — a bad cycle must not end the run
                self.stats.errors += 1
                log.exception("Cycle failed")

            if self._consecutive_failures >= self.config.poll.circuit_breaker_failures:
                log.error(
                    "Circuit breaker: %d consecutive failures. Pausing.",
                    self._consecutive_failures,
                )
                self._notify_breaker()
                self._consecutive_failures = 0
                self._cooldown = self.config.poll.cooldown_max_sec

            if once or time.time() >= deadline:
                break

            wait = self._cooldown or self._interval()
            self._cooldown = 0.0
            self._sleep(wait)

        self._finish()
        return 0

    def _sleep(self, seconds: float) -> None:
        """Sleep in slices so a stop signal is acted on promptly."""
        end = time.time() + seconds
        while time.time() < end and not self._stop:
            time.sleep(min(1.0, end - time.time()))

    def _notify_breaker(self) -> None:
        """Tell the user the poller is blind, rather than failing silently.

        Actions logs are ephemeral and nobody watches them; a scraper that has
        been quietly blocked for a week looks exactly like a quiet market.
        """
        from ..notify.base import Alert
        try:
            self.notifier.send(Alert(
                item_id=0, title="Vinted poller is being blocked — check the workflow logs.",
                brand_title="poller", price=0.0, currency="GBP", url="", image_url="",
                size="", condition="", heat=0.0, favourites=None, views=None,
                fav_per_hour=None, view_per_hour=None, age_minutes=0.0, measured=True,
            ))
        except Exception:  # noqa: BLE001
            pass

    def _finish(self) -> None:
        self.state.commit()
        summary = self.stats.as_markdown(self.bar, channel=self.config.alerts.channel)
        log.info("\n%s", summary)
        step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if step_summary:
            try:
                with open(step_summary, "a", encoding="utf-8") as handle:
                    handle.write(summary + "\n")
            except OSError as exc:
                log.warning("Could not write step summary: %s", exc)


def build_poller(config: Config, *, dry_run: bool, hot_db: Path) -> Poller:
    client = VintedClient(config)
    brands = resolve_brands(client, config.brands)
    log.info(
        "Resolved %d/%d brands; watching %d categories.",
        len(brands.ids), len(config.brands), len(config.categories),
    )
    poller = Poller(
        config=config,
        state=HotState(hot_db),
        client=client,
        notifier=build_notifier(config, dry_run=dry_run),
        brands=brands,
        dry_run=dry_run,        # also suppresses pushing the site feed
    )
    poller.load_baselines()
    return poller


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll Vinted for hot listings.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print alerts instead of pushing them")
    parser.add_argument("--once", action="store_true",
                        help="run a single cycle and exit")
    parser.add_argument("--max-runtime", type=float, default=None,
                        help="seconds to run before exiting cleanly")
    parser.add_argument("--hot-db", type=Path, default=DEFAULT_HOT_DB)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config()
    if not args.dry_run and config.alerts.enabled and config.alerts.channel == "ntfy":
        if not config.alerts.ntfy_topic:
            log.error("alerts.ntfy_topic is not set in config.yaml. Refusing to start.")
            return 2

    poller = build_poller(config, dry_run=args.dry_run, hot_db=args.hot_db)
    signal.signal(signal.SIGTERM, poller.request_stop)
    signal.signal(signal.SIGINT, poller.request_stop)
    try:
        return poller.run(once=args.once, max_runtime=args.max_runtime)
    finally:
        poller.state.close()


if __name__ == "__main__":
    sys.exit(main())

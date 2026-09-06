"""Deciding that a listing has disappeared — which usually means it sold.

Fast polling makes this observable for the first time. The daily scrape could
only ever say "unseen for five days"; at a seven-minute revisit interval we can
say "gone within four minutes", which is both the most useful thing the site can
tell you and the ground truth that would let every threshold in this system be
fitted to data instead of guessed.

The rule has to be careful. "It wasn't in the last response" is wrong: the sweep
reads only the first page, so on a busy category a perfectly healthy listing
falls off the end simply because newer ones pushed it down. Using that as the
test would mark most of the catalog sold.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tracked:
    """A listing we are watching, and when it was listed."""
    item_id: int
    listed_ts: int | None


def detect_gone(tracked: list[Tracked], returned_ids: set[int],
                returned_listed_ts: list[int]) -> list[int]:
    """Which tracked listings should have been on this page but weren't.

    Results come back `newest_first`, so the page holds the N newest listings in
    the category. If a tracked listing is *newer* than the oldest thing on the
    page, it falls inside the range the page covers — so its absence is real, not
    an artifact of pagination. That single comparison is what separates "sold"
    from "pushed off page one".

    Anything without a listing timestamp is left alone: we cannot place it in the
    ordering, and wrongly reporting a live listing as sold is worse than missing
    a sale.
    """
    if not returned_listed_ts:
        # Nothing to compare against — an empty or timestamp-less page tells us
        # nothing about what should have been on it.
        return []

    oldest_on_page = min(returned_listed_ts)
    return [
        item.item_id
        for item in tracked
        if item.item_id not in returned_ids
        and item.listed_ts is not None
        and item.listed_ts > oldest_on_page
    ]

"""Publishing the feed to a single-commit orphan branch.

The site needs fresh data every few minutes, but committing it to the main branch
would add hundreds of commits a day. The mechanics of a branch that never grows
live in `src.orphan_branch`, which the database snapshot uses too; this module is
just the feed's serialisation and its "never take the poller down" contract.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..orphan_branch import GitError, push_file

log = logging.getLogger(__name__)

FEED_FILENAME = "hot.json"


def serialise(feed: dict) -> str:
    """Compact but stable JSON — sorted keys so identical data hashes identically."""
    return json.dumps(feed, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def write_feed(path: Path, feed: dict) -> Path:
    """Write the feed to disk (used for the committed fallback copy)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialise(feed), encoding="utf-8")
    return path


def publish_feed(
    feed: dict,
    *,
    branch: str,
    repo_dir: Path,
    remote: str = "origin",
    dry_run: bool = False,
) -> bool:
    """Force-push the feed as the sole commit on `branch`. Returns True if pushed.

    Never raises: a failed publish is logged and swallowed. Losing a site refresh
    must not take down the poller, which is the part that actually notifies you.
    """
    count = len(feed.get("items", []))
    if dry_run:
        log.info("[dry-run] Would publish %d items to '%s'.", count, branch)
        return False

    try:
        push_file(
            repo_dir,
            branch=branch,
            filename=FEED_FILENAME,
            content=serialise(feed),
            message=f"feed: {count} hot listings @ {feed.get('generated_at', '')}",
            remote=remote,
        )
    except GitError as exc:
        log.warning("Feed publish failed: %s", exc)
        return False

    log.info("Published %d listings to '%s'.", count, branch)
    return True

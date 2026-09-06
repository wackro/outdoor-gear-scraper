"""Publishing the feed to a single-commit orphan branch.

The site needs fresh data every few minutes, but committing it to the main branch
would add hundreds of commits a day to a repository whose `.git` is already
141 MB. So the feed lives on its own branch that is **force-pushed as a single
parentless commit**: each publish replaces the previous one, and history never
accumulates no matter how long this runs.

Everything here uses git plumbing (`hash-object`, `mktree`, `commit-tree`,
`update-ref`) rather than `add`/`commit`. That means it never touches the index or
the working tree, so a publish firing mid-cycle cannot disturb the checkout the
poller is running from — and it cannot race the daily job's commit, which is on a
different branch entirely.
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

FEED_FILENAME = "hot.json"
GIT_TIMEOUT_SEC = 60


def serialise(feed: dict) -> str:
    """Compact but stable JSON — sorted keys so identical data hashes identically."""
    return json.dumps(feed, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def write_feed(path: Path, feed: dict) -> Path:
    """Write the feed to disk (used for the committed fallback copy)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialise(feed), encoding="utf-8")
    return path


def _git(repo_dir: Path, *args: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        input=stdin, capture_output=True, text=True,
        timeout=GIT_TIMEOUT_SEC, check=True,
    )
    return result.stdout.strip()


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
    payload = serialise(feed)
    if dry_run:
        log.info("[dry-run] Would publish %d items to '%s'.",
                 len(feed.get("items", [])), branch)
        return False

    try:
        blob = _git(repo_dir, "hash-object", "-w", "--stdin", stdin=payload)
        tree = _git(repo_dir, "mktree", stdin=f"100644 blob {blob}\t{FEED_FILENAME}\n")
        # No -p: a parentless commit, so the branch never grows a history.
        commit = _git(
            repo_dir, "commit-tree", tree,
            "-m", f"feed: {len(feed.get('items', []))} hot listings "
                  f"@ {feed.get('generated_at', '')}",
        )
        _git(repo_dir, "update-ref", f"refs/heads/{branch}", commit)
        _git(repo_dir, "push", "--force", remote, f"{branch}:{branch}")
    except subprocess.CalledProcessError as exc:
        log.warning("Feed publish failed (%s): %s",
                    " ".join(exc.cmd[-3:]), (exc.stderr or "").strip()[:300])
        return False
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("Feed publish failed: %s", exc)
        return False

    log.info("Published %d listings to '%s'.", len(feed.get("items", [])), branch)
    return True

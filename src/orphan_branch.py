"""Storing a single file on a branch that never grows a history.

Two things in this repository need to hand a file between workflow runs without
adding a commit to `main` every time: the hot feed, which the poller republishes
every few minutes, and the SQLite database, which the daily scrape rewrites.
Committing either to `main` is what took `.git` past 260 MB.

So each lives alone on its own branch, **force-pushed as a single parentless
commit**: every publish replaces the previous one and the branch stays exactly
one commit deep no matter how long this runs.

Everything here is git plumbing (`hash-object`, `mktree`, `commit-tree`,
`update-ref`) rather than `add`/`commit`. That means it never touches the index
or the working tree, so a publish firing mid-run cannot disturb the checkout the
caller is working from.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

GIT_TIMEOUT_SEC = 300      # generous: pushing a 30 MB database is not instant


class GitError(RuntimeError):
    """A git command failed. Carries the trimmed stderr, which is what matters."""


def git(repo_dir: Path, *args: str, stdin: str | None = None) -> str:
    """Run git and return its stdout. Text mode -- not for binary payloads."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            input=stdin, capture_output=True, text=True,
            timeout=GIT_TIMEOUT_SEC, check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise GitError(
            f"git {' '.join(args[:2])}: {(exc.stderr or '').strip()[:300]}"
        ) from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise GitError(f"git {' '.join(args[:2])}: {exc}") from exc
    return result.stdout.strip()


def push_file(
    repo_dir: Path,
    *,
    branch: str,
    filename: str,
    message: str,
    content: str | None = None,
    path: Path | None = None,
    remote: str = "origin",
) -> None:
    """Force-push one file as the sole commit on `branch`. Raises on failure.

    Give either `content` (text, hashed from stdin) or `path` (any file,
    including binary). The path form exists because `hash-object --stdin` runs
    through a text pipe, which cannot carry a SQLite file.
    """
    if (content is None) == (path is None):
        raise ValueError("push_file needs exactly one of `content` or `path`.")

    if content is not None:
        blob = git(repo_dir, "hash-object", "-w", "--stdin", stdin=content)
    else:
        blob = git(repo_dir, "hash-object", "-w", "--", str(path))

    tree = git(repo_dir, "mktree", stdin=f"100644 blob {blob}\t{filename}\n")
    # No -p: a parentless commit, so the branch never accumulates history.
    commit = git(repo_dir, "commit-tree", tree, "-m", message)
    git(repo_dir, "update-ref", f"refs/heads/{branch}", commit)
    git(repo_dir, "push", "--force", remote, f"{branch}:{branch}")


def fetch_file(
    repo_dir: Path,
    *,
    branch: str,
    filename: str,
    dest: Path,
    remote: str = "origin",
) -> bool:
    """Write `branch`'s copy of `filename` to `dest`. False if the branch has none.

    A missing branch is a normal cold start, not an error -- nothing has been
    published yet -- so it returns False and leaves `dest` alone. Anything else
    raises.
    """
    try:
        git(repo_dir, "fetch", "--depth", "1", remote, f"{branch}:refs/remote-snapshot")
    except GitError as exc:
        log.info("No '%s' branch to fetch (%s).", branch, exc)
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".fetching")
    try:
        # Binary-safe and streamed: `git show` straight into the file, so a
        # 30 MB database never passes through a text pipe or sits in memory.
        with partial.open("wb") as handle:
            subprocess.run(
                ["git", "-C", str(repo_dir), "show", f"refs/remote-snapshot:{filename}"],
                stdout=handle, stderr=subprocess.PIPE,
                timeout=GIT_TIMEOUT_SEC, check=True,
            )
    except subprocess.CalledProcessError as exc:
        partial.unlink(missing_ok=True)
        log.info("Branch '%s' has no %s (%s).", branch, filename,
                 (exc.stderr or b"").decode(errors="replace").strip()[:200])
        return False
    except (subprocess.TimeoutExpired, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise GitError(f"fetching {filename} from {branch}: {exc}") from exc

    # Rename last, so a failed transfer can never leave a truncated database
    # sitting where the real one belongs.
    partial.replace(dest)
    return True

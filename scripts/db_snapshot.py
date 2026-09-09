"""Move the SQLite database in and out of a single-commit orphan branch.

    python -m scripts.db_snapshot pull      # before a run, to get the database
    python -m scripts.db_snapshot push      # after a run, to store it again

The database is the only persistent state this project has, and it used to be
committed to `main` on every daily run. That is what took `.git` past 260 MB:
each run added a fresh multi-megabyte blob that nothing would ever read again.
On the `db-snapshot` branch it is force-pushed as one parentless commit, so the
current copy is always available and no history accumulates.

`pull` is deliberately fussy about failure. A missing database is not an empty
database: `Database()` would happily create a new one and the scrape would then
write a fresh, empty file over the top of everything we have ever collected. So
a pull that finds nothing exits non-zero unless `--allow-missing` is passed,
which is only for the first run, before the branch exists.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.orphan_branch import GitError, fetch_file, push_file  # noqa: E402
from src.storage.db import DEFAULT_DB_PATH  # noqa: E402

log = logging.getLogger("db_snapshot")

BRANCH = "db-snapshot"
FILENAME = "vinted.db"
REPO_DIR = Path(__file__).resolve().parent.parent


def _size_mb(path: Path) -> float:
    return path.stat().st_size / 1e6


def pull(db_path: Path, *, allow_missing: bool, repo_dir: Path = REPO_DIR) -> int:
    if fetch_file(repo_dir, branch=BRANCH, filename=FILENAME, dest=db_path):
        log.info("Pulled %s (%.1f MB) from '%s'.", db_path, _size_mb(db_path), BRANCH)
        return 0

    # No branch yet. During the cutover the database is still committed in the
    # checkout, so seeding from it is correct and expected exactly once.
    if db_path.exists():
        log.warning(
            "No '%s' branch; using the copy already in the checkout (%.1f MB). "
            "Expected on the first run only.", BRANCH, _size_mb(db_path),
        )
        return 0

    if allow_missing:
        log.warning("No database anywhere; starting empty because --allow-missing.")
        return 0

    log.error(
        "No database on '%s' and none in the checkout. Refusing to continue: a "
        "scrape would create an empty one and overwrite every observation we "
        "have. Pass --allow-missing only if that is genuinely what you want.",
        BRANCH,
    )
    return 1


def push(db_path: Path, *, repo_dir: Path = REPO_DIR) -> int:
    if not db_path.exists():
        log.error("Nothing to push: %s does not exist.", db_path)
        return 1
    try:
        push_file(
            repo_dir,
            branch=BRANCH,
            filename=FILENAME,
            path=db_path,
            message=f"db: snapshot at {_size_mb(db_path):.1f} MB",
        )
    except GitError as exc:
        # Unlike the feed, this one is fatal. The feed can miss a refresh and
        # recover on the next cycle; a database that never gets stored means the
        # run's work is gone when the container does.
        log.error("Failed to push the database to '%s': %s", BRANCH, exc)
        return 1
    log.info("Pushed %s (%.1f MB) to '%s'.", db_path, _size_mb(db_path), BRANCH)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=("pull", "push"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--allow-missing", action="store_true",
        help="on pull, start from an empty database instead of failing",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    if args.action == "pull":
        return pull(args.db, allow_missing=args.allow_missing)
    return push(args.db)


if __name__ == "__main__":
    sys.exit(main())

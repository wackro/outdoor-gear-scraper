"""Round-tripping a file through a branch that never grows a history.

The property that matters is that the branch stays one commit deep however many
times this runs, and that a binary file survives the trip byte for byte -- the
database is a SQLite file, and a text pipe would corrupt it silently.
"""
import sqlite3
import subprocess

import pytest

from src.orphan_branch import GitError, fetch_file, push_file


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A working repo with a bare remote, so pushes are real but local."""
    bare, work = tmp_path / "remote.git", tmp_path / "work"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        git(work, "config", key, value)
    (work / "README").write_text("x")
    git(work, "add", ".")
    git(work, "commit", "-qm", "init")
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "-q", "origin", "main")
    return work, bare


class TestPush:
    def test_text_content_lands_on_the_branch(self, repo):
        work, bare = repo
        push_file(work, branch="snap", filename="f.txt",
                  content="hello", message="m")
        assert git(bare, "ls-tree", "--name-only", "snap") == "f.txt"
        assert git(bare, "show", "snap:f.txt") == "hello"

    def test_a_file_lands_on_the_branch(self, repo):
        work, bare = repo
        src = work / "payload.bin"
        src.write_bytes(b"\x00\x01\x02binary\xff")
        push_file(work, branch="snap", filename="p.bin", path=src, message="m")
        assert git(bare, "ls-tree", "--name-only", "snap") == "p.bin"

    def test_the_branch_never_grows(self, repo):
        """The whole point. Ten publishes, still one commit."""
        work, bare = repo
        for i in range(10):
            push_file(work, branch="snap", filename="f.txt",
                      content=f"v{i}", message=f"m{i}")
        assert git(bare, "rev-list", "--count", "snap") == "1"
        assert git(bare, "show", "snap:f.txt") == "v9"

    def test_it_leaves_the_working_tree_and_index_alone(self, repo):
        """It runs while the caller is mid-scrape; it must not disturb them."""
        work, _ = repo
        (work / "scratch").write_text("uncommitted work")
        push_file(work, branch="snap", filename="f.txt", content="x", message="m")
        assert git(work, "status", "--porcelain") == "?? scratch"
        assert git(work, "rev-parse", "--abbrev-ref", "HEAD") == "main"

    def test_content_and_path_are_mutually_exclusive(self, repo):
        work, _ = repo
        with pytest.raises(ValueError, match="exactly one"):
            push_file(work, branch="snap", filename="f", message="m",
                      content="a", path=work / "README")
        with pytest.raises(ValueError, match="exactly one"):
            push_file(work, branch="snap", filename="f", message="m")

    def test_a_failed_push_raises(self, repo):
        work, _ = repo
        with pytest.raises(GitError):
            push_file(work, branch="snap", filename="f.txt", content="x",
                      message="m", remote="nonexistent")


class TestFetch:
    def test_a_missing_branch_is_not_an_error(self, repo):
        """A cold start: nothing has been published yet."""
        work, _ = repo
        dest = work / "data" / "out.bin"
        assert fetch_file(work, branch="never-pushed", filename="f",
                          dest=dest) is False
        assert not dest.exists()

    def test_a_missing_file_on_the_branch_is_not_an_error(self, repo):
        work, _ = repo
        push_file(work, branch="snap", filename="other.txt", content="x", message="m")
        assert fetch_file(work, branch="snap", filename="f.txt",
                          dest=work / "out.txt") is False

    def test_it_never_leaves_a_truncated_file_where_the_real_one_goes(self, repo):
        work, _ = repo
        dest = work / "data" / "existing.db"
        dest.parent.mkdir()
        dest.write_bytes(b"the good copy")
        assert fetch_file(work, branch="snap", filename="missing", dest=dest) is False
        assert dest.read_bytes() == b"the good copy", "clobbered a good database"


class TestRoundTrip:
    def test_a_sqlite_database_survives_byte_for_byte(self, repo):
        """Binary integrity is the requirement a text pipe would quietly break."""
        work, _ = repo
        source = work / "data" / "vinted.db"
        source.parent.mkdir()
        conn = sqlite3.connect(source)
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT)")
        conn.executemany("INSERT INTO items VALUES (?, ?)",
                         [(i, f"Item {i} — café Fjällräven") for i in range(500)])
        conn.commit()
        conn.close()
        original = source.read_bytes()

        push_file(work, branch="db-snapshot", filename="vinted.db",
                  path=source, message="db: snapshot")
        source.unlink()
        assert fetch_file(work, branch="db-snapshot", filename="vinted.db",
                          dest=source) is True

        assert source.read_bytes() == original
        conn = sqlite3.connect(source)
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 500
        assert conn.execute("SELECT title FROM items WHERE id = 7").fetchone()[0] \
            == "Item 7 — café Fjällräven"
        conn.close()


class TestPullSafety:
    """`pull` refusing to proceed is the guard on every observation we hold.

    `Database()` creates a file if none exists, and the scrape then writes a
    fresh empty one. So "no database found" must stop the run, not start it.

    Every case passes `repo_dir` explicitly. Left to its default these would run
    git against this repository and its real remote, which is both a network
    dependency in an offline suite and a thing that has gone wrong here before.
    """

    def test_it_refuses_to_start_from_nothing(self, repo, tmp_path, caplog):
        from scripts.db_snapshot import pull
        work, _ = repo
        missing = tmp_path / "data" / "vinted.db"
        assert pull(missing, allow_missing=False, repo_dir=work) == 1
        assert not missing.exists()
        assert "Refusing to continue" in caplog.text

    def test_allow_missing_is_the_deliberate_override(self, repo, tmp_path):
        from scripts.db_snapshot import pull
        work, _ = repo
        assert pull(tmp_path / "vinted.db", allow_missing=True, repo_dir=work) == 0

    def test_a_database_in_the_checkout_is_accepted_for_the_cutover(self, repo, tmp_path):
        from scripts.db_snapshot import pull
        work, _ = repo
        existing = tmp_path / "vinted.db"
        existing.write_bytes(b"pretend database")
        assert pull(existing, allow_missing=False, repo_dir=work) == 0
        assert existing.read_bytes() == b"pretend database"

    def test_the_snapshot_on_the_branch_wins_over_the_checkout(self, repo, tmp_path):
        """After the cutover the branch is the source of truth."""
        from scripts.db_snapshot import BRANCH, FILENAME, pull, push
        work, _ = repo
        good = tmp_path / "good.db"
        good.write_bytes(b"the real database")
        assert push(good, repo_dir=work) == 0

        stale = tmp_path / "vinted.db"
        stale.write_bytes(b"a stale checkout copy")
        assert pull(stale, allow_missing=False, repo_dir=work) == 0
        assert stale.read_bytes() == b"the real database"

    def test_pushing_a_database_that_is_not_there_fails(self, repo, tmp_path):
        from scripts.db_snapshot import push
        work, _ = repo
        assert push(tmp_path / "absent.db", repo_dir=work) == 1

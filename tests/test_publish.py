"""Publishing the feed to a force-pushed orphan branch.

The property that matters is that history never grows: this runs every few
minutes for as long as the poller lives, against a repo whose .git is already
large.
"""
import json
import subprocess

import pytest

from src.hot.publish import publish_feed, serialise, write_feed


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


def feed(n=1):
    return {"generated_at": f"t{n}", "items": [{"id": n}]}


def test_publishes_the_feed(repo):
    work, bare = repo
    assert publish_feed(feed(), branch="hot-feed", repo_dir=work) is True
    assert git(bare, "ls-tree", "--name-only", "hot-feed") == "hot.json"
    assert json.loads(git(bare, "show", "hot-feed:hot.json"))["items"] == [{"id": 1}]


def test_history_never_grows(repo):
    work, bare = repo
    for i in range(5):
        publish_feed(feed(i), branch="hot-feed", repo_dir=work)
    # The whole reason for the orphan-branch approach.
    assert git(bare, "rev-list", "--count", "hot-feed") == "1"
    assert json.loads(git(bare, "show", "hot-feed:hot.json"))["items"] == [{"id": 4}]


def test_leaves_the_working_tree_and_branch_alone(repo):
    work, _ = repo
    publish_feed(feed(), branch="hot-feed", repo_dir=work)
    # Uses git plumbing, so a publish mid-cycle cannot disturb the checkout the
    # poller is running from.
    assert git(work, "status", "--short") == ""
    assert git(work, "branch", "--show-current") == "main"


def test_does_not_touch_the_main_branch(repo):
    work, bare = repo
    publish_feed(feed(), branch="hot-feed", repo_dir=work)
    assert git(bare, "rev-list", "--count", "main") == "1"


def test_dry_run_publishes_nothing(repo):
    work, bare = repo
    assert publish_feed(feed(), branch="hot-feed", repo_dir=work, dry_run=True) is False
    with pytest.raises(subprocess.CalledProcessError):
        git(bare, "rev-parse", "hot-feed")


def test_a_git_failure_returns_false_rather_than_raising(tmp_path):
    # Losing a site refresh must never stop the poller.
    assert publish_feed(feed(), branch="hot-feed", repo_dir=tmp_path / "nope") is False


def test_serialisation_is_stable(tmp_path):
    a = serialise({"b": 1, "a": 2})
    b = serialise({"a": 2, "b": 1})
    assert a == b        # sorted keys, so identical data hashes identically

    path = write_feed(tmp_path / "sub" / "hot.json", {"x": 1})
    assert json.loads(path.read_text()) == {"x": 1}


def test_non_ascii_survives(repo):
    work, bare = repo
    publish_feed({"items": [{"brand_title": "Fjällräven"}]},
                 branch="hot-feed", repo_dir=work)
    assert "Fjällräven" in git(bare, "show", "hot-feed:hot.json")

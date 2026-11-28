"""New card branches must be cut from ``origin/main``, never from the
canonical clone's ``HEAD``.

The clone's ``HEAD`` drifts onto stale feature branches (observed 5754
commits behind on the fleet's ``~/work/hermes-agent``), and
``_ensure_git_worktree`` used to cut ``worktree add -b <branch> <target>
HEAD``, so every card branch inherited that stale history (t_bdc80dd8).
The base contract is now: fetch ``main`` first (bounded timeout), prefer
``origin/main``, fall back to local ``main``, and raise loudly when
neither exists — never a silent ``HEAD`` fallback.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db_workspace as kbw


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


def _canonical_clone(tmp_path: Path, branch: str = "main") -> Path:
    """A clone with a bare origin, ``branch`` pushed, identity configured."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", branch, str(origin))
    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", branch)
    _git(clone, "remote", "add", "origin", str(tmp_path / "origin.git"))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    (clone / "README.md").write_text("base\n", encoding="utf-8")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-m", "init")
    _git(clone, "push", "-u", "origin", branch)
    return clone


def test_new_branch_bases_on_origin_main_not_stale_head(tmp_path: Path) -> None:
    """Card-branch merge-base with origin/main equals the main tip even when
    the canonical clone sits on a stale feature branch."""
    clone = _canonical_clone(tmp_path)
    _git(clone, "checkout", "-b", "stale/feature")
    (clone / "stale.txt").write_text("side work\n", encoding="utf-8")
    _git(clone, "add", "stale.txt")
    _git(clone, "commit", "-m", "side-branch work")
    side_tip = _git(clone, "rev-parse", "HEAD")
    origin_main = _git(clone, "rev-parse", "origin/main")
    assert side_tip != origin_main

    target = clone / ".worktrees" / "t_base1111"
    kbw._ensure_git_worktree(clone, target, "wt/t_base1111")

    assert _git(target, "merge-base", "HEAD", "origin/main") == origin_main
    # The side branch's commit must not have ridden into the card branch.
    assert side_tip not in _git(target, "rev-list", "HEAD").split()


def test_new_branch_fetches_origin_main(tmp_path: Path) -> None:
    """A second clone advancing origin/main must show up in the card branch:
    the base ref is fetched, not read from a stale local remote-tracking ref."""
    clone = _canonical_clone(tmp_path)
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(tmp_path / "origin.git"), str(other))
    _git(other, "config", "user.email", "t@example.com")
    _git(other, "config", "user.name", "t")
    (other / "new.txt").write_text("landed\n", encoding="utf-8")
    _git(other, "add", "new.txt")
    _git(other, "commit", "-m", "landed on main")
    _git(other, "push", "origin", "main")
    moved_tip = _git(other, "rev-parse", "main")

    target = clone / ".worktrees" / "t_fetch2222"
    kbw._ensure_git_worktree(clone, target, "wt/t_fetch2222")

    assert _git(target, "rev-parse", "HEAD") == moved_tip


def test_falls_back_to_local_main_when_no_origin(tmp_path: Path) -> None:
    """No origin remote at all: the local ``main`` branch is the base."""
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    main_tip = _git(repo, "rev-parse", "main")
    _git(repo, "checkout", "-b", "stale/topic")
    (repo / "side.txt").write_text("side\n", encoding="utf-8")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-m", "side work")
    side_tip = _git(repo, "rev-parse", "HEAD")

    target = repo / ".worktrees" / "t_local3333"
    kbw._ensure_git_worktree(repo, target, "wt/t_local3333")

    assert _git(target, "merge-base", "HEAD", "main") == main_tip
    assert side_tip not in _git(target, "rev-list", "HEAD").split()


def test_raises_when_neither_origin_nor_local_main_exists(tmp_path: Path) -> None:
    """A master-only repo gets a loud failure, not a HEAD-based branch."""
    repo = tmp_path / "masteronly"
    repo.mkdir()
    _git(repo, "init", "-b", "master")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")

    with pytest.raises(RuntimeError) as excinfo:
        kbw._ensure_git_worktree(repo, repo / ".worktrees" / "t_none4444", "wt/t_none4444")
    assert "main" in str(excinfo.value)

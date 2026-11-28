"""Full clones inside task workspaces are flagged loudly at completion.

Project- and board-bound cards get a managed git worktree; a worker that
``git clone``s a repo inside its workspace instead defeats that lifecycle
(gigabytes per card, stale bases, worktrees-of-worktrees). Completion
(t_bdc80dd8) scans the workspace for nested full clones — ``.git``
DIRECTORIES whose clone root sits at most two directories below the
workspace root — and records a ``workspace_full_clone`` board event with
the path and size in MB plus a comment on the card. Detection is loud but
never a hard failure; a linked worktree's ``.git`` FILE is not a clone.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


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


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "init")
    _git(seed, "push", "-u", "origin", "main")
    return origin


def _complete_workspace_task(conn, workspace: Path) -> str:
    tid = kb.create_task(
        conn, title="workspace audit", assignee="worker",
        workspace_kind="scratch", workspace_path=str(workspace),
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    assert kb.complete_task(conn, tid, summary="done")
    return tid


def _clone_events(conn, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'workspace_full_clone'",
        (task_id,),
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def test_full_clone_in_workspace_flagged_at_completion(
    kanban_home: Path, origin_repo: Path
) -> None:
    ws = kanban_home / "ws"
    ws.mkdir()
    clone = ws / "side-proj"
    _git(ws, "clone", str(origin_repo), str(clone))

    with kbc.connect_closing() as conn:
        tid = _complete_workspace_task(conn, ws)
        events = _clone_events(conn, tid)
        by_path = {e["path"]: e for e in events}
        assert str(clone) in by_path, f"clone not flagged; events={events}"
        assert isinstance(by_path[str(clone)]["size_mb"], (int, float))
        assert by_path[str(clone)]["size_mb"] >= 0
        comments = conn.execute(
            "SELECT author, body FROM task_comments WHERE task_id = ?", (tid,)
        ).fetchall()
        flagged = [c for c in comments if "workspace_full_clone" in c["body"]]
        assert flagged, "no board comment naming the full clone"
        assert str(clone) in flagged[0]["body"]


def test_nested_deep_clone_beyond_depth_two_is_ignored(
    kanban_home: Path, origin_repo: Path
) -> None:
    ws = kanban_home / "ws"
    (ws / "a" / "b").mkdir(parents=True)
    deep = ws / "a" / "b" / "deep-proj"
    _git(ws / "a" / "b", "clone", str(origin_repo), str(deep))

    with kbc.connect_closing() as conn:
        tid = _complete_workspace_task(conn, ws)
        assert _clone_events(conn, tid) == []
        assert conn.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? "
            "AND body LIKE '%workspace_full_clone%'", (tid,)
        ).fetchone() is None


def test_linked_worktree_pointer_is_not_a_clone(
    kanban_home: Path, origin_repo: Path
) -> None:
    """A linked worktree checkout inside the workspace has a .git FILE —
    the managed lifecycle shape, never flagged."""
    ws = kanban_home / "ws"
    ws.mkdir()
    parent_clone = ws / "parent"
    _git(ws, "clone", str(origin_repo), str(parent_clone))
    wt = ws / "card-wt"
    _git(parent_clone, "worktree", "add", "-b", "wt/inner", str(wt), "HEAD")

    with kbc.connect_closing() as conn:
        tid = _complete_workspace_task(conn, ws)
        events = _clone_events(conn, tid)
        paths = {e["path"] for e in events}
        assert wt not in {Path(p) for p in paths}
        assert str(parent_clone) in paths  # the true clone beside it is flagged

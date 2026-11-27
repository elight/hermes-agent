"""A stale quota/auth stamp must not guard a card forever (t_b13a4839).

Live on gpud (t_75c0a3cb / t_d889f53e): a review card's reviewer hit a
provider quota wall; the stamp landed in ``tasks.last_failure_error``; later
runs ended normally (provider probed healthy) — but no clear path fired, so
``blocker_auth`` guarded the card forever. ``unblock`` refuses non-blocked
statuses, so even the manual escape hatch was closed, and the escalation
sentinel never matches a guard reason. These tests pin the supersession rule:
a latest run that ended with the worker/reviewer having done its job retires
the stamp; a latest failed run keeps it armed.

New tests live in this folder per the code-health ratchet (test_kanban_db.py
is over FILE_LINES and may only go down).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _stamp_run(conn, tid: str, *, outcome: str, ended_at: int,
               stamp: str | None) -> None:
    """Run + task-row shape exactly as production leaves them after a
    rate-limited requeue / failed run: the stamp stays on the task."""
    claim = kb.claim_task(conn, tid)
    assert claim is not None
    conn.execute(
        "UPDATE task_runs SET outcome=?, status=?, ended_at=? WHERE id=?",
        (outcome, outcome, ended_at, claim.current_run_id),
    )
    conn.execute(
        "UPDATE tasks SET status='ready', current_run_id=NULL, "
        "claim_lock=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?",
        (stamp, tid),
    )
    conn.commit()


@pytest.mark.parametrize("superseding_outcome",
                         ["completed", "review_requested", "changes_requested"])
def test_stale_auth_stamp_superseded_by_later_run(kanban_home, superseding_outcome):
    """A stamp from a SUPERSEDED run must not guard: the latest run closed as
    a successful handoff, so the quota text is history, not the current
    condition — the forever-park on gpud had no self-clear path otherwise."""
    now = 5_000_000
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="stale stamp", assignee="a")
        _stamp_run(conn, tid, outcome="rate_limited", ended_at=now,
                   stamp="pid 1 exited rate-limited (quota wall) — requeued")
        _stamp_run(conn, tid, outcome=superseding_outcome, ended_at=now + 500,
                   stamp="pid 1 exited rate-limited (quota wall) — requeued")

        assert kbd.check_respawn_guard(conn, tid) is None
        assert kbd.check_respawn_guard(conn, tid, lane="review") is None


def test_auth_stamp_holds_after_failure_flavoured_latest_run(kanban_home):
    """Amnesty is for successful handoffs only: the latest run failed and the
    quota stamp stays armed (the breaker still trips eventually)."""
    now = 5_000_000
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="real blocker", assignee="a")
        _stamp_run(conn, tid, outcome="timed_out", ended_at=now,
                   stamp="provider authentication failed (403)")
        assert kbd.check_respawn_guard(conn, tid) == "blocker_auth"

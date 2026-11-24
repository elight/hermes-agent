"""create_task must refuse to write a task row that names no creator.

Census ruling (t_6406066c): creator-less rows on a live board all came from a
raw ``kb.create_task(conn, title=...)`` library call, while every real surface
— the kanban_create tool, the CLI, the dashboard, the swarm builder — stamps
an actor into ``created_by``. A write with no actor is the signature of a
stray script, so the DB layer fails closed before anything is written and
names the board file the call would have hit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_create_task_refuses_missing_or_blank_created_by(kanban_home):
    """No actor (None or whitespace) is refused, nothing lands, error names the board file."""
    with kbc.connect() as conn:
        db_file = conn.execute("PRAGMA database_list").fetchone()[2]
        assert db_file  # file-backed board, so the refusal can name it

        with pytest.raises(ValueError, match="created_by") as exc:
            kb.create_task(conn, title="stray probe")
        assert db_file in str(exc.value)

        for blank in ("", "   ", "\t"):
            with pytest.raises(ValueError, match="created_by"):
                kb.create_task(conn, title="blank probe", created_by=blank)

        # Fail closed means fail empty: no task rows, no audit events, no runs.
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0


def test_create_task_still_writes_with_an_actor(kanban_home):
    """The gate must not touch the normal path: an actor still writes the row."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="owned", assignee="worker", created_by="alice")
        assert kb.get_task(conn, tid).created_by == "alice"

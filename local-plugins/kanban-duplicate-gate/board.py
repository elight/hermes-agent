"""Read the open cards that share a title, out of the board the tool is about to write to.

The plugin decides on data, not on guesses: it opens the same board database
the ``kanban_create`` handler will open, read-only, and asks for the rows the
rule cares about. ``board_db_path`` resolves through the same private helper
the tool itself uses, so the plugin and the writer can never disagree about
which board is in play (see README.md for why that import is deliberate).

Every function here is best-effort: a board that is missing, locked, or not
yet initialised yields no candidates, which means no refusal.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

OPEN_STATUSES_EXCLUDED = ("done", "archived")

_OPEN_BY_TITLE = (
    "SELECT id, body FROM tasks WHERE lower(trim(title)) = lower(trim(?)) "
    "AND status NOT IN ({placeholders}) ORDER BY created_at"
)
_KEY_LOOKUP = "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' LIMIT 1"


def board_db_path(board: Optional[str] = None) -> Optional[Path]:
    """Path of the board's ``kanban.db``, resolved exactly as the tool resolves it."""
    from hermes_cli.kanban_db import kanban_db_path

    return Path(kanban_db_path(board=board))


def _connect(db_path: Path) -> Optional[sqlite3.Connection]:
    """Read-only connection to an existing board DB; ``None`` when it is not there.

    ``query_only`` rather than ``mode=ro``: a live board is in WAL mode, and a
    read-only URI open cannot always create the ``-shm`` it needs. The pragma
    keeps this connection from ever writing while leaving WAL access intact.
    """
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def _rows(db_path: Path, sql: str, params: tuple) -> list[sqlite3.Row]:
    """Run one read; an unreadable or un-initialised board reads as no rows."""
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        return list(conn.execute(sql, params).fetchall())
    except sqlite3.Error as exc:  # missing table, locked, corrupt: never break the create
        logger.debug("duplicate-gate: read on %s failed: %s", db_path, exc)
        return []
    finally:
        conn.close()


def open_cards_with_title(db_path: Path, title: str) -> list[tuple[str, Optional[str]]]:
    """``(id, body)`` of every open card whose title matches, oldest first."""
    placeholders = ",".join("?" * len(OPEN_STATUSES_EXCLUDED))
    rows = _rows(db_path, _OPEN_BY_TITLE.format(placeholders=placeholders),
                 (title, *OPEN_STATUSES_EXCLUDED))
    return [(str(row["id"]), row["body"]) for row in rows]


def has_idempotency_key(db_path: Path, key: str) -> bool:
    """True when a live card already carries this idempotency key.

    The in-core gate ran the idempotency lookup first and returned the existing
    card; at the tool boundary the refusal happens before the handler runs, so
    the gate has to leave a keyed repeat alone for the handler to return the
    same card.
    """
    return bool(_rows(db_path, _KEY_LOOKUP, (key,)))

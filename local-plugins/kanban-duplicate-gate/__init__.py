"""kanban-duplicate-gate — refuse a duplicate ``kanban_create`` at the tool boundary.

Registered as a ``pre_tool_call`` hook (``hermes_cli/plugins.py`` VALID_HOOKS;
fired by ``model_tools._pre_dispatch_guards`` before ``registry.dispatch``). When
a create arrives whose title matches an open card and whose body is not
materially different, the hook returns ``{"action": "block", "message": ...}``:
the handler never runs, no row is written, and the message becomes the tool
result the caller sees.

Behaviour, coverage and the one private import this reaches through are
documented in README.md. The rule itself lives in :mod:`gate` (pure) and the
board reads in :mod:`board` (read-only SQLite).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from . import board, gate

logger = logging.getLogger(__name__)

TOOL_NAME = "kanban_create"


def refusal(args: dict[str, Any]) -> Optional[str]:
    """Refusal message for this create, or ``None`` to let the handler run."""
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        return None  # a missing title is the handler's own validation, not a duplicate

    db_path = board.board_db_path(args.get("board"))
    if db_path is None:
        return None

    key = args.get("idempotency_key")
    if isinstance(key, str) and key and board.has_idempotency_key(db_path, key):
        return None  # a keyed repeat is the same card: let the handler return it

    open_cards = board.open_cards_with_title(db_path, title)
    found = gate.find_duplicate(open_cards, title, args.get("body")) if open_cards else None
    if found is None:
        return None
    logger.debug("kanban-duplicate-gate: %r duplicates %s", title.strip(), found[0])
    return gate.message(found[0], title.strip(), found[1])


def on_pre_tool_call(tool_name: str = "", args: Optional[dict[str, Any]] = None, **kwargs: Any) -> Optional[dict]:
    """``pre_tool_call`` callback: veto a duplicate card creation, ignore everything else."""
    if tool_name != TOOL_NAME or not isinstance(args, dict):
        return None
    try:
        message = refusal(args)
    except Exception as exc:
        # Deliberate catch-all boundary: the gate is policy on top of card
        # creation, so if it cannot decide it must not decide — the create is
        # allowed, loudly, with the traceback.
        logger.warning("kanban-duplicate-gate: check failed, allowing the create: %s", exc, exc_info=True)
        return None
    if message is None:
        return None
    return {"action": "block", "message": message}


def register(ctx: Any) -> None:
    """Plugin entry point: subscribe the gate to the pre-tool-call veto."""
    ctx.register_hook("pre_tool_call", on_pre_tool_call)

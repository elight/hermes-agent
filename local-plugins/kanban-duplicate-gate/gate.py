"""The duplicate-card rule, as pure functions — no Hermes import, no I/O.

Ported from the working-tree gate that ran uncommitted in
``hermes_cli/kanban_db.py`` (see the card's ``duplicate-card-gate.patch``); the
arithmetic and the normalisation are copied verbatim so the plugin decides
exactly as the in-core version did.

Rule: a new card is refused when an *open* card has the same title (compared
case-insensitively, surrounding whitespace trimmed) and a body that is not
materially different. Bodies are compared after card ids, timestamps and every
number are removed and whitespace is collapsed; a similarity of
``DUPLICATE_BODY_SIMILARITY`` (0.90) or more is a duplicate.
"""

from __future__ import annotations

import difflib
import re
from typing import Iterable, Optional, Sequence, Tuple

# Same title + body at least this similar (after stripping ids, timestamps and numbers) = duplicate.
DUPLICATE_BODY_SIMILARITY = 0.90

# "Open" is every status except these two: a blocked or triage card still blocks a refile.
DONE_OR_ARCHIVED = ("done", "archived")

_CARD_ID = re.compile(r"\bt_[0-9a-f]{6,}\b")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?z?")
_NUMBER = re.compile(r"\d+(?:\.\d+)?(?:e[+-]?\d+)?")
_WHITESPACE = re.compile(r"\s+")


class DuplicateCardError(ValueError):
    """A new card has the same title as an open card and a body that is not materially different."""

    def __init__(self, existing_id: str, title: str, similarity: float):
        self.existing_id = existing_id
        self.title = title
        self.similarity = similarity
        super().__init__(message(existing_id, title, similarity))


def message(existing_id: str, title: str, similarity: float) -> str:
    """Refusal text: names the open card, and the three ways out of the refusal."""
    return (
        f"duplicate card: {existing_id} is already open with the identical title {title!r} and a "
        f"description that is not materially different (similarity {similarity:.2f} >= "
        f"{DUPLICATE_BODY_SIMILARITY:.2f}). Comment on {existing_id} instead, or give this card a "
        f"different title or a materially different description. Nothing was created."
    )


def normalize_body(text: Optional[str]) -> str:
    """Lower-case, drop card ids / timestamps / numbers, collapse whitespace."""
    t = (text or "").lower()
    t = _CARD_ID.sub(" ", t)  # card ids
    t = _TIMESTAMP.sub(" ", t)  # timestamps
    t = _NUMBER.sub(" ", t)  # numbers: counts, ports, values
    return _WHITESPACE.sub(" ", t).strip()


def body_similarity(new_body: Optional[str], old_body: Optional[str]) -> float:
    """1.0 when the normalised bodies are equal, else difflib's ratio."""
    new = normalize_body(new_body)
    old = normalize_body(old_body)
    if new == old:
        return 1.0
    return difflib.SequenceMatcher(None, new, old, autojunk=False).ratio()


def find_duplicate(
    open_cards: Iterable[tuple[str, Optional[str]]], title: str, body: Optional[str]
) -> Optional[tuple[str, float]]:
    """``(id, similarity)`` of the first candidate that is not materially different, else ``None``.

    ``open_cards`` must already be narrowed to the same title and to open
    statuses — that narrowing is the board query's job (``board.open_cards``).
    """
    for card_id, card_body in open_cards:
        similarity = body_similarity(body, card_body)
        if similarity >= DUPLICATE_BODY_SIMILARITY:
            return str(card_id), similarity
    return None


def check(
    open_cards: Sequence[tuple[str, Optional[str]]], title: str, body: Optional[str]
) -> None:
    """Raise :class:`DuplicateCardError` when ``open_cards`` already holds this work."""
    found = find_duplicate(open_cards, title, body)
    if found is not None:
        raise DuplicateCardError(found[0], title.strip(), found[1])

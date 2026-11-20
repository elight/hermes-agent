"""``hermes providers`` — which provider paths are closed, and how to reopen one.

The operator's surface for the per-path provider lock (``agent/provider_locks``). Wording is
plain on purpose: a path is locked, and a healthy provider is reopened — no circuit-breaker
vocabulary (Evan, 2026-11-20).

    hermes providers                          # what is closed, why, and for how long
    hermes providers --reopen claude-sub      # scriptable reopen
    hermes providers --json                   # for other tooling
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional

from agent.provider_locks import (
    ProviderLock, age_text, clear_lock, list_locks, locks_payload, stamp_utc,
)


def _row(lock: ProviderLock) -> str:
    """One locked path as the operator reads it: what, why, for how long, who recorded it."""
    closed = f"locked {age_text(lock.age_seconds)} ago"
    until = (f"reopens itself at {stamp_utc(lock.expires_at)}" if lock.expires_at
             else "stays locked until you reopen it")
    lines = [f"  {lock.path}  ({closed}; {until})",
             f"    class:  {lock.lock_class}",
             f"    reason: {lock.reason}"]
    if lock.source:
        lines.append(f"    from:   {lock.source}")
    if lock.component:
        lines.append(f"    set by: {lock.component}")
    lines.append(f"    reopen: hermes providers --reopen {lock.path}")
    return "\n".join(lines)


def _payload(locks: list[ProviderLock]) -> dict[str, Any]:
    """The machine-readable form of the same listing (``--json``; the dashboard uses the same)."""
    return locks_payload(locks)


def _reopen(name: str) -> int:
    """Clear one path. Exit status is the scripting answer: 0 reopened, 1 nothing was locked."""
    try:
        reopened = clear_lock(name)
    except ValueError as exc:  # several paths share the name — the operator picks one
        print(f"hermes providers: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"hermes providers: {exc}", file=sys.stderr)
        return 2
    if not reopened:
        print(f"hermes providers: no provider path is locked under the name {name!r}.", file=sys.stderr)
        print("Run `hermes providers` to see what is locked.", file=sys.stderr)
        return 1
    print(f"Reopened {name}. Its next request will go out again.")
    return 0


def _offer_reopen(locks: list[ProviderLock]) -> None:
    """Pick a locked path to reopen. TTY only — a non-interactive run just lists and exits."""
    if not sys.stdin.isatty():
        print("\nReopen one: hermes providers --reopen <path>")
        return
    from hermes_cli.setup import _curses_prompt_choice
    choices = [f"{lock.path} — {lock.lock_class}: {lock.reason} (locked {age_text(lock.age_seconds)} ago)"
               for lock in locks] + ["Cancel"]
    index = _curses_prompt_choice("Reopen which provider path?", choices, len(choices) - 1)
    if index is None or index >= len(locks):
        print("Left everything locked.")
        return
    _reopen(locks[index].path)


def cmd_providers(args) -> int:
    """``hermes providers``: list the locked paths, or reopen one with ``--reopen``."""
    name: Optional[str] = getattr(args, "reopen", None)
    if name:
        return _reopen(name)
    locks = list_locks()
    if getattr(args, "json", False):
        print(json.dumps(_payload(locks), indent=2))
        return 0
    if not locks:
        print("No provider paths are locked. Every configured provider is being dialed normally.")
        return 0
    print(f"{len(locks)} provider path(s) locked — a locked path is not dialed at all:")
    for lock in locks:
        print(_row(lock))
    print("\nA lock is set deliberately, after repeated failures, so nothing keeps retrying a dead "
          "provider. Only reopen one once the provider is healthy again.")
    if not getattr(args, "no_select", False):
        _offer_reopen(locks)
    return 0

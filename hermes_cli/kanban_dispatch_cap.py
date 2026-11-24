"""What the ``kanban.*`` dispatch cap keys MEAN, resolved the same way on every path.

Four places start a dispatcher pass, and until now each answered "what is this profile's
cap" for itself:

* ``gateway/kanban_watchers_dispatcher.py`` — the gateway tick, resolves
  ``kanban.max_in_progress_per_profile`` into ``_DispatcherSettings`` at boot.
* ``hermes_cli/kanban_ops.py::_cmd_dispatch`` — ``hermes kanban dispatch``, reads the same key.
* ``hermes_cli/kanban_db_dispatch.py::run_daemon`` — the deprecated standalone loop, which
  passed no per-profile cap at all despite its own docstring ("the standalone daemon must not
  be the one uncapped entry point").
* ``plugins/kanban/dashboard/plugin_api.py::dispatch`` — the dashboard's nudge button, which
  passed none either, at ``max=8`` by default.

Two of those four disagreed with the documented contract. ``kanban.max_in_progress_per_profile``
defaults to *unset (unlimited)* (``website/docs/user-guide/features/kanban.md``) and the gateway
reads it exactly that way, so on a box where the key IS set the dashboard button and the daemon
loop still started one profile's queue unchecked.

This module invents no default. Unset means unlimited on every path, which is what the docs and
the gateway already promise; the defect was entry points that ignored the key, not the key's
default. A config that could not be READ is a separate case from a key that was never written,
and the CLI's forced pass refuses to dispatch on that one — see ``_cmd_dispatch`` — matching how
``kanban.dispatch_profiles`` already treats a failed config read as fail-closed rather than
permissive.

It owns the meaning of the cap keys and nothing else: no database, no board, no spawn. Every
dispatch entry point calls it before it opens a connection, so they cannot drift apart again.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Why a caller got the cap it got, so each entry point can say so out loud. "unset" is the one
# an operator most needs printed: it is the difference between "nothing else started because of
# a cap" and "everything started because no cap was ever configured".
SOURCE_CONFIGURED = "configured"
SOURCE_OPTED_OUT = "opted-out"
SOURCE_UNSET = "unset"


def resolve_profile_cap(configured: Any) -> tuple[Optional[int], str]:
    """Resolve one value of ``kanban.max_in_progress_per_profile`` → ``(cap, source)``.

    ``cap`` of ``None`` means no per-profile limit, and it comes from the documented default
    (key absent) or the documented opt-out (``0``) — never from a path that simply never
    consulted the key.

    ``configured`` is whatever the config yielded: an int, a numeric string, ``0``, or ``None``.
    Anything unparseable is reported as unset and stays unlimited, matching
    ``kanban.max_in_progress``'s documented "invalid or below-1 values behave as unlimited".
    """
    if isinstance(configured, bool):
        # ``True``/``False`` are ints in Python. A YAML ``max_in_progress_per_profile: true`` is
        # not a cap of one; treat it as the invalid value it is instead of honouring it.
        return None, SOURCE_UNSET
    if isinstance(configured, int):
        value = configured
    else:
        try:
            value = int(str(configured).strip())
        except (TypeError, ValueError):
            return None, SOURCE_UNSET
    if value > 0:
        return value, SOURCE_CONFIGURED
    if value == 0:
        return None, SOURCE_OPTED_OUT
    return None, SOURCE_UNSET


def configured_profile_cap() -> Any:
    """``kanban.max_in_progress_per_profile`` exactly as written, or None when unset.

    Deliberately raw. ``0`` is a decision and an unparseable value is an accident; the caller
    has to be able to tell them apart, which is what :func:`resolve_profile_cap` is for — so
    this returns what the file said, not what it meant.
    """
    return _kanban_setting("max_in_progress_per_profile")


def configured_max_in_progress() -> Optional[int]:
    """Read ``kanban.max_in_progress`` from config, or None when unset/invalid.

    Shared so every dispatch entry point agrees on "explicitly configured": a positive
    integer wins, anything else falls through to the derived default.

    LIVES HERE, NOT IN THE DISPATCHER. ``kanban_db_dispatch`` is over its file-size cap and
    can only shrink, and a cap read does not belong scattered one per entry point anyway —
    this module owns what the ``kanban.*`` cap keys mean. It is re-exported from
    ``kanban_db_dispatch``, where ``run_daemon`` and the gateway still resolve the name, and
    where tests keep patching it.
    """
    raw = _kanban_setting("max_in_progress")
    if raw is None:
        return None
    try:
        ival = int(raw)
    except (TypeError, ValueError):
        return None
    return ival if ival >= 1 else None


def _kanban_setting(key: str) -> Any:
    """One key from the ``kanban:`` block, or None when it is not there. Read-only.

    A dispatcher pass should not die because a config file is unhappy, so a failed read is
    logged with its traceback and reported as "not configured". Callers that must not guess on
    a failed read — the CLI's forced pass — do their OWN read and refuse on error; this helper
    is for the paths that only need the value.
    """
    try:
        from hermes_cli.config import FailedConfigRead, load_config_readonly
        raw = load_config_readonly() or {}
        if isinstance(raw, FailedConfigRead):
            # "Nothing configured" and "I could not read the file" must not be the same line in
            # the log: this path keeps dispatching on a broken config on purpose, so a silent
            # None here is how a cap goes missing without a trace.
            logger.warning("kanban: config.yaml could not be read (unreadable, not unset); "
                           "kanban.%s is being treated as unset for this pass", key)
            return None
        kanban = (raw.get("kanban") or {})
        return kanban.get(key)
    except Exception:  # health: allow BLE001 -- config-read boundary; any failure means "unset", logged below
        logger.warning("kanban: could not read kanban.%s from config; treating it as unset",
                       key, exc_info=True)
        return None


def resolve_entry_point_cap() -> tuple[Optional[int], str]:
    """Read the config and resolve it in one step — what a dispatch path calls."""
    return resolve_profile_cap(configured_profile_cap())

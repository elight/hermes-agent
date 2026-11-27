"""Per-path provider locks — the durable, shared "this egress path is closed" record.

Why this exists (2026-11-20). One Claude account hit its monthly spend limit. The refusal arrived
wrapped in an HTTP 502, so it classified as a transient server error, and every session on the
host kept its own private backoff while sharing one dead path: retries piled up until hermes-serve's
event loop starved and dropped every app socket. Per-session backoff cannot fix that, because the
dead thing is shared. A lock makes the fact shared too: one file per egress path, on disk, that
every process on this host obeys before it dials.

Unit: the EGRESS PATH, not the account. ``gpud`` consolidates the Anthropic accounts behind one
subscription path, so Hermes tracks paths (``openrouter``, ``claude-subscription``, …) and never
mirrors account state.

State: ``<hermes home>/providers/<path>.lock``. Existence = closed; the contents are for display.
Created atomically and never clobbered (the first failure to close a path owns the record);
``billing`` locks never expire, transient locks carry an expiry from the provider's own reset hint.
Reopening — the only way back — deletes the file.

Enforcement is deliberately NOT centralised in hermes-serve: that night serve was the sick
component while healthy backends hammered the dead path. Every dial site calls :func:`dial`
itself, so a locked path fails fast in the caller's own process, with no upstream request and no
retry loop.

TODO(v2, cross-host gateways): same-host file state is all v1 reads, and deliberately so —
file locking across NFS is unsound (Evan, 2026-11-20). The agreed next step is auto-detection at
helper start-up: same host → read the lock file; remote gateway → ask serve over HTTP and cache
the answer for ~5 s. No install-time question for the user. Nothing here builds that yet.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A rate-limited 429 that names a reset at least this far out is a window nobody should sit
# through N times over N sessions; below it, per-request backoff is already the right tool and a
# lock would only punish healthy traffic. Transient locks are the exception, never the rule.
TRANSIENT_LOCK_MIN_SECONDS = 300.0
# How much of the provider's own words the record keeps (display + the terminal message).
_SOURCE_MAX_CHARS = 300
_REASON_MAX_CHARS = 120
_LOCK_SUFFIX = ".lock"
_UNSAFE = re.compile(r"[^a-z0-9._-]+")

BILLING_CLASS = "billing"


@dataclass(frozen=True)
class ProviderLock:
    """One closed path. ``expires_at`` is None for a lock that waits for a human."""

    path: str
    lock_class: str
    reason: str
    source: str
    locked_at: float
    component: str
    expires_at: Optional[float] = None

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.locked_at)

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= time.time()

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "class": self.lock_class, "reason": self.reason,
                "source": self.source, "locked_at": self.locked_at, "component": self.component,
                "expires_at": self.expires_at}


class ProviderPathLocked(RuntimeError):
    """Raised at a dial site instead of opening a request on a closed path.

    It is classified as provider unavailability, so the caller's existing fallback chain takes the
    turn; when the chain has nowhere left to go, the terminal chat copy explains the locks."""

    def __init__(self, lock: ProviderLock, message: str = ""):
        super().__init__(message or f"provider path locked until cleared: "
                                    f"{lock.lock_class} {lock.reason} at {stamp_utc(lock.locked_at)}")
        self.lock = lock


def stamp_utc(epoch: float) -> str:
    """A lock's moment in UTC, minute-precision: ``2026-11-20 00:52Z``."""
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M") + "Z"


def _clip(text: Optional[str], limit: int) -> str:
    collapsed = " ".join(str(text or "").split())
    return collapsed[:limit].rstrip()


def path_id(provider: str, auth: str = "") -> str:
    """The on-disk identity of an egress path: a slug, never a caller-supplied filename.

    Sanitized so a provider name can't escape the lock directory. ``auth`` extends it only when a
    caller states the discriminator (two auth modes on one provider bill separately); empty or
    unusable input has no path, which means "nothing is known to be closed"."""
    parts = [str(provider or "").strip().lower(), str(auth or "").strip().lower()]
    slug = "-".join(p for p in (_UNSAFE.sub("_", part).strip("_-.") for part in parts) if p)
    return slug


def lock_dir() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "providers"


def lock_file(path: str) -> Path:
    return lock_dir() / f"{path}{_LOCK_SUFFIX}"


def _parse_lock_file(file: Path) -> Optional[ProviderLock]:
    """Read one record; an unreadable or expired one is removed and reads as open."""
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Another process reopened the path between the directory scan and this read: a
        # healthy race, not a corrupt record. Say nothing.
        return None
    try:
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        lock = ProviderLock(
            path=str(raw.get("path") or file.name[: -len(_LOCK_SUFFIX)]),
            lock_class=str(raw.get("class") or BILLING_CLASS),
            reason=_clip(raw.get("reason"), _REASON_MAX_CHARS) or "no reason recorded",
            source=_clip(raw.get("source"), _SOURCE_MAX_CHARS),
            locked_at=float(raw.get("locked_at") or 0.0),
            component=str(raw.get("component") or ""),
            expires_at=float(raw["expires_at"]) if raw.get("expires_at") is not None else None,
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        logger.warning("Ignoring unreadable provider lock %s: %s", file.name, exc)
        file.unlink(missing_ok=True)
        return None
    if lock.expired:
        # A transient window closed while nobody was looking: the path is dialable again.
        file.unlink(missing_ok=True)
        return None
    return lock


def read_lock(path: str) -> Optional[ProviderLock]:
    if not path:
        return None
    return _parse_lock_file(lock_file(path))


def list_locks() -> list[ProviderLock]:
    """Every closed path, stable-ordered, for ``hermes providers`` and the dashboard."""
    try:
        files = sorted(lock_dir().glob(f"*{_LOCK_SUFFIX}"))
    except OSError as exc:
        logger.debug("Provider lock directory unreadable: %s", exc)
        return []
    return [lock for lock in (_parse_lock_file(f) for f in files) if lock is not None]


def resolve_path(name: str) -> Optional[str]:
    """Map what an operator typed to one locked path: the exact name, else its unique path.

    ``openrouter`` and ``claude-subscription`` are paths in their own right; ``anthropic`` names
    ``anthropic-oauth`` when that is the only Anthropic path locked. Two candidates is a question
    for the operator, not a coin flip."""
    if not name:
        return None
    wanted = path_id(name)
    if read_lock(wanted) is not None:
        return wanted
    candidates = ambiguous_path(name)
    return candidates[0] if len(candidates) == 1 else None


def ambiguous_path(name: str) -> list[str]:
    """Live locked paths named exactly, or under, what an operator typed.

    An expired record still on disk never counts, so a stale file cannot make a clean name
    ambiguous."""
    wanted = path_id(name)
    if not wanted:
        return []
    prefix = f"{wanted}-"
    return [lock.path for lock in list_locks()
            if lock.path == wanted or lock.path.startswith(prefix)]


def create_lock(*, provider: str, lock_class: str, reason: str, source: str = "",
                component: str = "", ttl_seconds: Optional[float] = None,
                auth: str = "") -> Optional[ProviderLock]:
    """Close a path. Returns the lock, or None when it was already closed (or could not be).

    First writer wins: the record that explains the storm stays the record, and later, noisier
    failures cannot rewrite it. Never raises — a home that will not take the file costs the lock,
    not the reply."""
    path = path_id(provider, auth)
    if not path or not lock_class:
        return None
    now = time.time()
    lock = ProviderLock(path=path, lock_class=lock_class, reason=_clip(reason, _REASON_MAX_CHARS)
                        or "no reason recorded", source=_clip(source, _SOURCE_MAX_CHARS),
                        locked_at=now, component=component,
                        expires_at=now + ttl_seconds if ttl_seconds else None)
    try:
        lock_file(path).parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("Cannot create the provider lock directory: %s", exc)
        return None
    if not _place_exclusively(lock_file(path), json.dumps(lock.as_dict(), indent=1) + "\n"):
        return None
    logger.warning("Provider path locked: %s (%s — %s) by %s", path, lock_class, lock.reason, component)
    return lock


def _place_exclusively(target: Path, payload: str) -> bool:
    """Write to a temp file, then rename/link it into place without ever clobbering a lock."""
    tmp = target.with_name(f".{target.stem}.{os.getpid()}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        try:
            os.link(tmp, target)  # atomic, and fails when a lock already exists
        except FileExistsError:
            return False
        except OSError:
            # No hard links on this filesystem (FAT, some network mounts): the exclusive
            # create is the best available no-clobber primitive.
            with open(target, "x", encoding="utf-8") as handle:
                handle.write(payload)
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        logger.debug("Could not write provider lock %s: %s", target, exc)
        return False
    finally:
        tmp.unlink(missing_ok=True)


def clear_lock(name: str) -> bool:
    """Reopen one path. True when a lock was removed; False when nothing was locked there.

    Raises ValueError when the name matches several locked paths: reopening the wrong one is an
    operator decision, not something to guess."""
    path = resolve_path(name)
    if path is None:
        candidates = ambiguous_path(name)
        if len(candidates) > 1:
            raise ValueError(f"{name!r} matches several locked paths: {', '.join(candidates)}")
        return False
    try:
        lock_file(path).unlink(missing_ok=True)
    except OSError as exc:
        raise OSError(f"could not reopen the provider path {path}: {exc}") from exc
    logger.warning("Provider path reopened: %s", path)
    return True


def locks_payload(locks: Optional[list[ProviderLock]] = None) -> dict[str, Any]:
    """The machine-readable listing, shared by ``hermes providers --json`` and the dashboard:
    the same facts the terminal prints, with the age already worked out."""
    return {"locked_paths": [
        {**lock.as_dict(), "age_seconds": round(lock.age_seconds), "age": age_text(lock.age_seconds),
         "text": describe(lock)}
        for lock in (list_locks() if locks is None else locks)]}


def age_text(seconds: float) -> str:
    """How long a path has been closed, in the two units an operator reads: ``3d 4h``, ``12m``."""
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else "under a minute"


def describe(lock: ProviderLock) -> str:
    """One closed path, in the shape the operator reads: name, class, reason, and the date that
    matters — when it closed, or until when a transient one stays closed."""
    when = stamp_utc(lock.expires_at) if lock.expires_at else stamp_utc(lock.locked_at)
    verb = "until" if lock.expires_at else "at"
    return f"{lock.path}: {lock.lock_class} — {lock.reason} {verb} {when}"


def dial(provider: str, auth: str = "", *, component: str = "") -> None:
    """The one question every dial site asks first: is this path open? Raises if it is not.

    The message names every closed path, because the operator needs the whole picture to decide
    what to reopen, and the caller's retry loop never sees a request it could repeat."""
    path = path_id(provider, auth)
    lock = read_lock(path) if path else None
    if lock is None:
        return
    message = (f"provider path locked until cleared: {lock.lock_class} {lock.reason} "
               f"at {stamp_utc(lock.locked_at)}")
    others = [other for other in list_locks() if other.path != lock.path]
    if others:
        # Name the dialed path too: the reader must see which one this turn was refused on.
        listing = "; ".join(describe(other) for other in [lock] + others)
        message += f" (locked paths: {listing})"
    if component:
        logger.info("%s dial refused on locked path %s", component, path)
    raise ProviderPathLocked(lock, message)


def maybe_lock_from_error(classified: Any, *, provider: str, auth: str = "",
                          component: str = "") -> Optional[ProviderLock]:
    """Close the path when the provider says the ACCOUNT is spent, never when it says 'slow down'.

    ``billing`` (a hard quota, exhausted credits, a spend cap) closes the path with no expiry:
    nothing Hermes can retry will lift it. A plain rate limit keeps its per-request backoff. Only
    a rate limit whose own body names a reset far enough out closes anything, and then only until
    that reset."""
    from agent.error_classifier import FailoverReason

    reason = getattr(classified, "reason", None)
    path = path_id(provider, auth)
    if not path or reason is None:
        return None
    message = _clip(getattr(classified, "message", ""), _SOURCE_MAX_CHARS)
    if reason is FailoverReason.billing:
        if getattr(classified, "billing_unverified", False):
            # #82154: the same body arrives for a content-filter rejection. The credential pool
            # takes a short cooldown on that; it is not proof the account is spent, so no path
            # closes on an unverified reading.
            return None
        return create_lock(provider=provider, auth=auth, lock_class=BILLING_CLASS,
                           reason=message or "the provider says the account cannot serve paid traffic",
                           source=message, component=component)
    if reason is FailoverReason.rate_limit:
        reset_at = (getattr(classified, "error_context", None) or {}).get("reset_at")
        if not isinstance(reset_at, (int, float)):
            return None
        ttl = reset_at - time.time()
        if ttl < TRANSIENT_LOCK_MIN_SECONDS:
            return None
        return create_lock(provider=provider, auth=auth, lock_class=FailoverReason.rate_limit.value,
                           reason=f"rate limited until {stamp_utc(reset_at)}", source=message,
                           component=component, ttl_seconds=ttl)
    return None


def all_paths_locked_copy(locks: list[ProviderLock]) -> str:
    """The in-chat sentence for a turn with nowhere left to go, or '' when a path is open.

    Three things it must say (Evan, 2026-11-20): which paths are closed and why; that this was
    deliberate, so it does not read as an outage to ride out by retrying; and the exact command
    that reopens a path once the provider is healthy again."""
    if not locks:
        return ""
    listing = "; ".join(describe(lock) for lock in locks)
    commands = ", or ".join(f"hermes providers --reopen {lock.path}" for lock in locks)
    return (
        f"All provider paths are locked out ({listing}). The locks were set deliberately, after "
        f"repeated failures, to prevent a retry storm — this is not an outage to ride out by "
        f"retrying. When a provider is healthy again, clear it: {commands} (list: hermes providers)."
    )

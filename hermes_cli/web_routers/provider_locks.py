"""Provider-path lock routes — the dashboard's view of locked provider paths, and its reopen.

hermes-serve owns this lifecycle surface. The lock itself is a file on disk that every process on
the host reads for itself before dialing (``agent.provider_locks``), so a serve restart never
forgets a lock, and a serve that is sick — as it was on 2026-11-20 — never lets a closed path
through, because serve was never the thing that stops a dial.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from agent.provider_locks import clear_lock, locks_payload

log = logging.getLogger("hermes_cli.web_server")
router = APIRouter()


class ReopenBody(BaseModel):
    path: str


@router.get("/api/provider-locks")
async def get_provider_locks():
    """Every locked provider path: class, reason, age, and who recorded it."""
    return await run_in_threadpool(locks_payload)


@router.post("/api/provider-locks/reopen")
async def post_provider_lock_reopen(body: ReopenBody):
    """Reopen one path. 409 when the name matches several locks — the caller must be specific."""
    try:
        reopened = await run_in_threadpool(clear_lock, body.path)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:  # a home we cannot write: say so rather than claim it reopened
        log.warning("Provider path reopen failed for %s: %s", body.path, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if not reopened:
        raise HTTPException(status_code=404,
                            detail=f"no provider path is locked under the name {body.path!r}")
    return {"reopened": body.path, **(await run_in_threadpool(locks_payload))}

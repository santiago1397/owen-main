"""`/api/agent-runtime/*` — the WRITE surface for agent runtimes (AI_AGENT_SPEC D13).

Deliberately NOT an extension of `/api/ai/*`. That surface publishes its guarantee in four
places, including `AI_API.md` and the scope descriptions: *"All read-only — nothing in this
API can mutate platform data."* Machine consumers read that to teach themselves the API.
Bolting writes onto it would quietly falsify documentation an agent is trusting.

So: same machinery, honest semantics. The `api_keys` table, `core/apikeys.py` hashing, scope
gating, per-key rate limiting and the `api_key_usage` audit trail are all reused; only the
scope (`agent_write`) and the surface are new.

This is also the seam D6 deferred — "other projects → OWEN". It is built now for one consumer
(owen-voice) rather than speculatively for many, and the multi-tenancy questions it eventually
raises (whose numbers, whose billing, whose transcripts) stay unanswered until something
actually needs them answered.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ai.deps import require_scope
from app.core.apikeys import SCOPE_AGENT_WRITE
from app.db import get_db
from app.agents.capture import normalise_capture
from app.models import Call, CallCapture, Caller, ContactNote

logger = logging.getLogger("api.agent_runtime")

router = APIRouter(prefix="/api/agent-runtime", tags=["agent-runtime"])


class CaptureIn(BaseModel):
    linkedid: str = Field(..., description="The call's provider_call_sid (Asterisk Linkedid).")
    fields: dict = Field(..., description="Captured values; core keys plus anything else.")
    capture_type: str = "lead"
    agent_version_id: Optional[str] = None


async def _call_for(db: AsyncSession, linkedid: str) -> Call:
    call = (
        await db.execute(select(Call).where(Call.provider_call_sid == linkedid))
    ).scalars().first()
    if call is None:
        raise HTTPException(404, f"no call with provider_call_sid {linkedid!r}")
    return call


@router.post("/captures")
async def create_capture(
    body: CaptureIn,
    db: AsyncSession = Depends(get_db),
    _key=Depends(require_scope(SCOPE_AGENT_WRITE)),
) -> dict:
    """Record structured data an agent collected (D7).

    Append-only and one row per event, so an agent that learns a name early and an address
    later produces two rows with their own timestamps rather than one overwritten guess."""
    call = await _call_for(db, body.linkedid)
    fields = normalise_capture(body.fields or {})
    if not fields:
        raise HTTPException(422, "no usable fields in the capture")

    av_id = None
    if body.agent_version_id:
        try:
            av_id = uuid.UUID(body.agent_version_id)
        except ValueError:
            raise HTTPException(422, "agent_version_id must be a uuid")
    # Fall back to whatever the call already has pinned, so a capture is attributable to a
    # concrete agent config even when the runtime does not bother to send one.
    av_id = av_id or call.agent_version_id

    row = CallCapture(
        call_id=call.id,
        agent_version_id=av_id,
        capture_type=body.capture_type or "lead",
        fields=fields,
    )
    db.add(row)
    await db.commit()
    logger.info("agent-runtime: capture stored for %s (%s)", body.linkedid, sorted(fields))
    return {"ok": True, "capture_id": str(row.id), "fields": sorted(fields)}


class NoteIn(BaseModel):
    linkedid: str
    body: str


@router.post("/notes")
async def create_note(
    body: NoteIn,
    db: AsyncSession = Depends(get_db),
    _key=Depends(require_scope(SCOPE_AGENT_WRITE)),
) -> dict:
    """Attach a free-text note to the CALLER of a call.

    Notes are additive and never touch `callers.label`, `company` or `role`: those are
    human-entered fields, and the platform's standing rule is that humans win over models."""
    call = await _call_for(db, body.linkedid)
    if call.caller_id is None:
        raise HTTPException(409, "that call has no caller to attach a note to")
    text = (body.body or "").strip()
    if not text:
        raise HTTPException(422, "note body is empty")
    note = ContactNote(caller_id=call.caller_id, body=text[:4000])
    db.add(note)
    await db.commit()
    return {"ok": True, "note_id": str(note.id)}


@router.get("/calls/{linkedid}")
async def call_context(
    linkedid: str,
    db: AsyncSession = Depends(get_db),
    _key=Depends(require_scope(SCOPE_AGENT_WRITE)),
) -> dict:
    """What a runtime needs to know about the call it is handling: who is on it, and what has
    already been captured, so a second agent on the same call does not ask twice."""
    call = await _call_for(db, linkedid)
    caller = None
    if call.caller_id is not None:
        caller = (
            await db.execute(select(Caller).where(Caller.id == call.caller_id))
        ).scalar_one_or_none()
    captures = (
        await db.execute(
            select(CallCapture).where(CallCapture.call_id == call.id)
            .order_by(CallCapture.captured_at)
        )
    ).scalars().all()
    return {
        "linkedid": linkedid,
        "call_id": str(call.id),
        "direction": call.direction,
        "started_at": call.started_at.isoformat() if call.started_at else None,
        "caller": None if caller is None else {
            "number": caller.phone_number,
            "label": caller.label,
            "company": caller.company,
            "total_calls": caller.total_calls,
        },
        "captures": [
            {"type": c.capture_type, "fields": c.fields,
             "at": c.captured_at.isoformat() if c.captured_at else None}
            for c in captures
        ],
    }

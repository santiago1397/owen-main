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


# --- CRM context (CRM_CONTEXT_SPEC C15/C16) ------------------------------------------------
# The built-in `ghl` adapter is exposed HERE, behind the same generic contract an external
# provider implements. That is what keeps owen-voice simple: it only ever speaks one protocol
# and POSTs to a URL, and whether a real CRM or one of OWEN's own adapters answers is a
# resolution detail it never sees. A future in-house CRM implements these two shapes and OWEN
# learns nothing about it.


class LookupIn(BaseModel):
    caller_number: str
    dialed_number: str = ""
    linkedid: str = ""


@router.post("/crm/lookup")
async def crm_lookup(
    body: LookupIn,
    _key=Depends(require_scope(SCOPE_AGENT_WRITE)),
) -> dict:
    """The built-in GHL adapter, in provider shape: `{display_name, summary, facts}`.

    Never raises and never 500s on a CRM problem — an unknown caller is a normal outcome, and
    this runs while someone is waiting to be greeted. A miss returns empty fields, and the
    agent simply opens with its generic greeting."""
    from app.providers import ghl_api

    contact = await ghl_api.find_contact_by_phone(body.caller_number)
    if not contact:
        return {"display_name": None, "summary": "", "facts": {}}

    name = " ".join(
        p for p in [contact.get("firstName"), contact.get("lastName")] if p
    ).strip() or contact.get("contactName") or contact.get("name")

    facts: dict = {}
    for src, dst in (("email", "email"), ("address1", "address"), ("city", "city"),
                     ("companyName", "company"), ("source", "lead_source")):
        if contact.get(src):
            facts[dst] = contact[src]
    for tag in ("tags",):
        if isinstance(contact.get(tag), list) and contact[tag]:
            facts["tags"] = ", ".join(str(t) for t in contact[tag][:5])

    # State, not just identity — the thing a CRM knows and OWEN does not (C9).
    opps = await ghl_api.contact_opportunities(contact.get("id") or "")
    open_opps = [o for o in opps if str(o.get("status") or "").lower() == "open"]
    summary_bits = []
    if open_opps:
        first = open_opps[0]
        summary_bits.append(
            f"{len(open_opps)} open opportunity"
            + ("s" if len(open_opps) > 1 else "")
            + (f", most recent '{first.get('name')}'" if first.get("name") else "")
        )
        if first.get("monetaryValue"):
            facts["open_value"] = str(first["monetaryValue"])
        if first.get("pipelineStageId"):
            facts["stage"] = str(first.get("stageName") or first["pipelineStageId"])
    return {
        "display_name": name or None,
        "summary": ". ".join(summary_bits),
        "facts": facts,
    }


class ReportIn(BaseModel):
    linkedid: str
    caller_number: str = ""
    outcome: str = ""
    duration_s: int | None = None
    captures: list = []
    transfer: str | None = None
    owen_url: str | None = None


@router.post("/crm/report")
async def crm_report(
    body: ReportIn,
    _key=Depends(require_scope(SCOPE_AGENT_WRITE)),
) -> dict:
    """The built-in GHL adapter for the write direction (C11/C12).

    Writes a TIMELINE ENTRY — outcome, what was captured, and a link back to OWEN — not the
    transcript. A 40-turn transcript in a CRM note is a worse copy of something OWEN already
    stores with the audio beside it, and GHL has no transcript search worth using.

    Uses the direct v2 API, never the webhook trigger: the dormant `GHL_CALL_WEBHOOK_URL`
    path costs a premium execution per completed call, and the token already in `.env.prod`
    does this free."""
    from app.providers import ghl_api

    contact = await ghl_api.find_contact_by_phone(body.caller_number)
    if not contact or not contact.get("id"):
        # Nothing to attach to. Deliberately not an error: an unknown caller is normal, and
        # creating a contact from a single call is a decision for the capture relay, not this.
        return {"ok": False, "reason": "no matching contact"}

    lines = [f"AI agent call{f' — {body.outcome}' if body.outcome else ''}."]
    if body.duration_s:
        lines.append(f"Duration: {body.duration_s}s.")
    merged: dict = {}
    for cap in body.captures or []:
        for k, v in (cap.get("fields") or {}).items():
            if k != "extra" and v not in (None, ""):
                merged[k] = v
    if merged:
        lines.append("Captured: " + ", ".join(f"{k}: {v}" for k, v in merged.items()) + ".")
    if body.transfer:
        lines.append(f"Transferred to {body.transfer}.")
    if body.owen_url:
        lines.append(body.owen_url)

    await ghl_api.add_contact_note(contact["id"], " ".join(lines))
    logger.info("agent-runtime: reported call %s to GHL contact %s", body.linkedid, contact["id"])
    return {"ok": True, "contact_id": contact["id"]}

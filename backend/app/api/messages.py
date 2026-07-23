"""Messages inbox read API (Ticket 09).

Backs the two-pane inbox: a thread list (grouped by number+caller) and a single thread's
messages. READ-ONLY — inbound ingestion stays in the webhooks; manual outbound send is a
later ticket (10). Threads are DERIVED (see services/message_threads.group_threads); no thread
table, no stored read-state. Additive alongside the existing /api/calls surface.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db import get_db
from app.models import Caller, Campaign, Message, Number, Provider, User
from app.services import queue, sms
from app.services.message_threads import group_threads
from app.services.messages import enqueue_outbound_message, get_optout_state
from app.services.number_sync import is_carrier_active

router = APIRouter(prefix="/api/messages", tags=["messages"])

# Same provider buckets the Calls page uses (Ticket 06): omit for all providers.
PROVIDER_GROUPS: dict[str, tuple[str, ...]] = {
    "attribution": ("twilio", "signalwire"),
    "platform": ("bulkvs", "asterisk"),
}

# Cap the rows scanned to build the thread list — an SMS inbox is small, and threads are
# derived in-process from newest-first rows.
_THREADS_SCAN_LIMIT = 2000


def _msg_columns():
    return (
        Message.id,
        Message.number_id,
        Message.caller_id,
        Message.direction,
        Message.body,
        Message.received_at,
        Provider.name.label("provider"),
        Caller.phone_number.label("caller_number"),
        Number.phone_number.label("number_phone"),
        Number.friendly_name.label("number_label"),
        Number.sms_enabled.label("sms_enabled"),
        Number.sms_campaign_id.label("sms_campaign_id"),
        Campaign.name.label("campaign_name"),
    )


def _joined(stmt):
    return (
        stmt.join(Provider, Message.provider_id == Provider.id, isouter=True)
        .join(Caller, Message.caller_id == Caller.id, isouter=True)
        .join(Number, Message.number_id == Number.id, isouter=True)
        .join(Campaign, Message.campaign_id == Campaign.id, isouter=True)
    )


@router.get("/threads")
async def list_threads(
    provider_group: str | None = Query(
        None, description="Optional bucket: 'attribution' (twilio/signalwire) or 'platform' (bulkvs/asterisk)"
    ),
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    stmt = _joined(select(*_msg_columns()))
    names = PROVIDER_GROUPS.get(provider_group) if provider_group else None
    if names:
        stmt = stmt.where(Provider.name.in_(names))
    stmt = stmt.order_by(Message.received_at.desc()).limit(_THREADS_SCAN_LIMIT)

    rows = (await db.execute(stmt)).mappings().all()
    # group_threads is duck-typed; feed it attribute-style row views.
    threads = group_threads([_Row(r) for r in rows])
    return [
        {
            "number_id": t.number_id,
            "caller_id": t.caller_id,
            "caller_number": t.caller_number,
            "number_phone": t.number_phone,
            "number_label": t.number_label,
            "campaign_name": t.campaign_name,
            "provider": t.provider,
            "last_body": t.last_body,
            "last_direction": t.last_direction,
            "last_at": t.last_at,
            "message_count": t.message_count,
            "sms_enabled": t.sms_enabled,
            # Why the composer is disabled (None when the number may send) — Ticket 10.
            "sms_disabled_reason": sms.outbound_block_reason(t.sms_enabled, t.sms_campaign_id),
        }
        for t in threads
    ]


@router.get("/thread")
async def get_thread(
    number_id: uuid.UUID | None = None,
    caller_id: uuid.UUID | None = None,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """One conversation's messages, oldest-first (chat order). NULL number_id/caller_id is a
    valid thread key (e.g. an inbound to an unregistered DID), matched explicitly."""
    stmt = _joined(select(*_msg_columns(), Message.num_media, Message.media_urls, Message.status))
    stmt = stmt.where(
        Message.number_id == number_id if number_id is not None else Message.number_id.is_(None)
    )
    stmt = stmt.where(
        Message.caller_id == caller_id if caller_id is not None else Message.caller_id.is_(None)
    )
    stmt = stmt.order_by(Message.received_at.asc())
    rows = (await db.execute(stmt)).mappings().all()
    return [
        {
            "id": r["id"],
            "direction": r["direction"],
            "body": r["body"],
            "status": r["status"],
            "num_media": r["num_media"],
            "media_urls": r["media_urls"] or [],
            "received_at": r["received_at"],
            "caller_number": r["caller_number"],
            "number_phone": r["number_phone"],
            "number_label": r["number_label"],
            "provider": r["provider"],
        }
        for r in rows
    ]


class SendBody(BaseModel):
    number_id: uuid.UUID
    contact: str  # external party's E.164 number (the thread's caller)
    body: str


@router.post("/send")
async def send_message(
    payload: SendBody,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Operator replies to a thread from a 10DLC-enabled BulkVS DID (Ticket 10). Writes an
    outbound `messages` row (direction='outbound', sent_by_user_id) and enqueues a
    `message_send` job. REFUSES (409) when the number isn't sms_enabled / has no campaign, or
    the contact has opted out. The actual BulkVS send + GHL relay happen in the worker."""
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "message body is required")

    number = await db.get(Number, payload.number_id)
    if number is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "number not found")

    # Per-number 10DLC gate.
    reason = sms.outbound_block_reason(number.sms_enabled, number.sms_campaign_id)
    if reason:
        raise HTTPException(status.HTTP_409_CONFLICT, reason)

    # Carrier gate: a DID still provisioning at BulkVS (e.g. a SUBMITTED port-in) cannot
    # be used for any operation until /tnRecord reports it Active.
    if not is_carrier_active(number.provider_status):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"number is not active at the carrier yet (status: {number.provider_status})",
        )

    # Per-contact opt-out.
    state = await get_optout_state(db, number.id, payload.contact)
    if sms.is_opted_out(state):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "recipient has opted out of SMS from this number"
        )

    msg = await enqueue_outbound_message(db, number, payload.contact, body, user.id)
    await queue.enqueue(db, "message_send", {"message_id": str(msg.id)})
    return {"id": str(msg.id), "status": msg.status, "direction": msg.direction}


class _Row:
    """Adapt a SQLAlchemy mappings() row to the attribute access group_threads expects."""

    def __init__(self, m):
        self.number_id = m["number_id"]
        self.caller_id = m["caller_id"]
        self.body = m["body"]
        self.direction = m["direction"]
        self.received_at: datetime | None = m["received_at"]
        self.caller_number = m["caller_number"]
        self.number_phone = m["number_phone"]
        self.number_label = m["number_label"]
        self.campaign_name = m["campaign_name"]
        self.provider = m["provider"]
        self.sms_enabled = m["sms_enabled"]
        self.sms_campaign_id = m["sms_campaign_id"]

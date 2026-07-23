"""Messages inbox read API (Ticket 09).

Backs the two-pane inbox: a thread list (grouped by number+caller) and a single thread's
messages. READ-ONLY — inbound ingestion stays in the webhooks; manual outbound send is a
later ticket (10). Threads are DERIVED (see services/message_threads.group_threads); no thread
table, no stored read-state. Additive alongside the existing /api/calls surface.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db import get_db
from app.models import Caller, Campaign, Message, Number, Provider, User
from app.services.message_threads import group_threads

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

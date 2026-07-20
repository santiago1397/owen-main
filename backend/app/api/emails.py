"""Inbound-email observability API (JWT-authed).

Lets you see what the mailbox poller ingested: parsed vs failed, whether each was relayed
to GHL, and the extracted fields. `parse_status=failed` is the queue of emails a human
should inspect (the template changed, or a field was missing) — nothing there was relayed.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db import get_db
from app.models import InboundEmail, User

router = APIRouter(prefix="/api/emails", tags=["emails"])


def _summary(e: InboundEmail) -> dict:
    return {
        "id": str(e.id),
        "message_id": e.message_id,
        "source": e.source,
        "from_addr": e.from_addr,
        "subject": e.subject,
        "job_id": e.job_id,
        "parse_status": e.parse_status,
        "parse_error": e.parse_error,
        "relayed_to_ghl": e.relayed_to_ghl,
        "relayed_at": e.relayed_at.isoformat() if e.relayed_at else None,
        "received_at": e.received_at.isoformat() if e.received_at else None,
    }


@router.get("")
async def list_emails(
    parse_status: str | None = Query(None, description="'parsed' | 'failed'"),
    relayed: bool | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    where = []
    if parse_status:
        where.append(InboundEmail.parse_status == parse_status)
    if relayed is not None:
        where.append(InboundEmail.relayed_to_ghl.is_(relayed))

    total = (
        await db.execute(select(func.count()).select_from(InboundEmail).where(*where))
    ).scalar_one()
    rows = (
        await db.execute(
            select(InboundEmail).where(*where)
            .order_by(InboundEmail.received_at.desc())
            .limit(limit).offset(offset)
        )
    ).scalars().all()
    return {"total": total, "items": [_summary(e) for e in rows]}


@router.get("/{email_id}")
async def get_email(
    email_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    e = await db.get(InboundEmail, email_id)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "email not found")
    return {**_summary(e), "to_addr": e.to_addr, "fields": e.fields, "raw": e.raw}

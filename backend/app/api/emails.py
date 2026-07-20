"""Inbound-email observability API (JWT-authed).

Powers the frontend "Email Log": every email the mailbox poller ingested, whether it
parsed, and the truthful outcome of its GHL relay attempt (sent / skipped-not-configured /
failed). `parse_status=failed` is the human-inspect queue (template changed / field missing)
— nothing there is ever relayed. The detail view exposes `ghl_payload`: the exact JSON that
was (or would be) POSTed to GoHighLevel.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.config import settings
from app.db import get_db
from app.models import InboundEmail, User
from app.services import emails, queue

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
        "relay_status": e.relay_status,
        "relay_error": e.relay_error,
        "relay_result": e.relay_result,
        "relayed_at": e.relayed_at.isoformat() if e.relayed_at else None,
        "received_at": e.received_at.isoformat() if e.received_at else None,
    }


@router.get("")
async def list_emails(
    parse_status: str | None = Query(None, description="'parsed' | 'failed'"),
    relay_status: str | None = Query(None, description="'sent' | 'skipped_not_configured' | 'failed'"),
    relayed: bool | None = None,
    limit: int = Query(50, le=200),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    where = []
    if parse_status:
        where.append(InboundEmail.parse_status == parse_status)
    if relay_status:
        where.append(InboundEmail.relay_status == relay_status)
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
    # Surface whether the GHL email relay is configured, so the UI can explain
    # 'skipped_not_configured' rows without a second request.
    return {
        "total": total,
        "items": [_summary(e) for e in rows],
        "ghl_email_relay_configured": bool(settings.ghl_api_enabled or settings.GHL_EMAIL_WEBHOOK_URL),
        "ghl_relay_mode": "api" if settings.ghl_api_enabled else ("webhook" if settings.GHL_EMAIL_WEBHOOK_URL else None),
    }


@router.get("/{email_id}")
async def get_email(
    email_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    e = await db.get(InboundEmail, email_id)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "email not found")
    return {
        **_summary(e),
        "to_addr": e.to_addr,
        "fields": e.fields,
        # The exact payload sent (or that would be sent) to GHL — only meaningful for parsed.
        "ghl_payload": emails.ghl_payload(e) if e.parse_status == "parsed" else None,
        "ghl_email_relay_configured": bool(settings.ghl_api_enabled or settings.GHL_EMAIL_WEBHOOK_URL),
        "ghl_relay_mode": "api" if settings.ghl_api_enabled else ("webhook" if settings.GHL_EMAIL_WEBHOOK_URL else None),
        "raw": e.raw,
    }


@router.post("/{email_id}/relay")
async def relay_email(
    email_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(current_user),
) -> dict:
    """Manually (re-)enqueue a parsed email for GHL relay — used to flush
    'skipped_not_configured'/'failed' rows once the webhook URL is set."""
    e = await db.get(InboundEmail, email_id)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "email not found")
    if e.parse_status != "parsed":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "email is not parsed; nothing to relay")
    if e.relayed_to_ghl:
        return {"status": "already_relayed"}
    e.relay_status = "pending"
    e.relay_error = None
    await db.commit()
    await queue.enqueue(db, "email_relay_ghl", {"email_id": str(e.id)})
    return {"status": "enqueued"}

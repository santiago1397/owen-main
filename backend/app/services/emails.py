"""Inbound-email ingestion.

An email pulled from the mailbox is parsed and upserted keyed on the RFC Message-ID for
idempotency (re-polling the same message is safe). The raw email is always stored. Only
*successfully parsed* emails get a GHL relay job enqueued; parse failures are stored with
parse_status='failed' + parse_error and are never relayed (the agreed failure policy).

Returns (row, created) so the poller enqueues a relay job exactly once — on first insert.
"""

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import InboundEmail
from app.providers.dispatch_email import ParsedEmail
from app.services.mailbox import FetchedEmail

logger = logging.getLogger("ingestion")


async def ingest_email(
    db: AsyncSession, msg: FetchedEmail, parsed: ParsedEmail, source: str
) -> tuple[InboundEmail, bool]:
    """Idempotent-insert one email. `created` is False if this Message-ID was seen before."""
    result = await db.execute(
        pg_insert(InboundEmail)
        .values(
            message_id=msg.message_id,
            source=source,
            from_addr=msg.from_addr,
            to_addr=msg.to_addr,
            subject=msg.subject,
            job_id=parsed.job_id,
            parse_status="parsed" if parsed.ok else "failed",
            parse_error=parsed.error,
            fields=parsed.fields or None,
            raw=msg.raw,
            received_at=msg.received_at,
        )
        # Never reprocess a Message-ID we've already stored (protects the relay-once guard
        # even if the mailbox re-delivers or \Seen wasn't set).
        .on_conflict_do_nothing(index_elements=["message_id"])
        .returning(InboundEmail.id)
    )
    inserted_id = result.scalar_one_or_none()
    await db.commit()

    row = (
        await db.execute(
            select(InboundEmail).where(InboundEmail.message_id == msg.message_id)
        )
    ).scalar_one()
    created = inserted_id is not None
    logger.info(
        "ingest_email: message_id=%s job_id=%s parse_status=%s created=%s",
        msg.message_id, parsed.job_id, row.parse_status, created,
    )
    return row, created

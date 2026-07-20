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


def _derived_fields(f: dict) -> dict:
    """Flatten the nested extracted data into top-level scalars GHL's no-code webhook mapper
    can bind directly (it can't reach into `items`/`payment`/`contacts` arrays), plus a
    ready-to-use `job_description` for an Opportunity note. All defensive — absent inputs
    just omit the derived key."""
    d: dict = {}

    items = f.get("items") or []
    if items:
        first = items[0] or {}
        d["problem"] = first.get("problem") or first.get("title")
        d["item_title"] = first.get("title")
        d["item_status"] = first.get("status")

    pay = f.get("payment") or {}
    for k in ("total", "paid", "remaining"):
        if pay.get(k) is not None:
            d[f"payment_{k}"] = pay[k]

    notes = f.get("coverage_notes") or []
    if notes:
        d["coverage_notes_text"] = "; ".join(notes)

    if f.get("customer_phone"):
        d["primary_contact_phone"] = f["customer_phone"]

    # A human-readable one-glance summary to drop into a GHL Opportunity note / SMS.
    lines = []
    header = " ".join(x for x in [f.get("job_id"), f.get("service")] if x)
    if header:
        prio = f.get("priority")
        lines.append(f"Job {header}" + (f" ({prio} priority)" if prio else ""))
    if f.get("customer_name"):
        lines.append(f"Customer: {f['customer_name']}")
    contact_bits = [b for b in [f.get("customer_phone"), f.get("customer_email")] if b]
    if contact_bits:
        lines.append(" / ".join(contact_bits))
    if f.get("service_address"):
        lines.append(f"Address: {f['service_address']}")
    if d.get("problem"):
        prob = f"Problem: {d.get('item_title')} — {d['problem']}" if d.get("item_title") else f"Problem: {d['problem']}"
        lines.append(prob)
    if pay.get("total") is not None:
        pay_line = f"Payment: total ${pay.get('total')}, paid ${pay.get('paid', '?')}, remaining ${pay.get('remaining', '?')}"
        if f.get("brand"):
            pay_line += f" ({f['brand']})"
        lines.append(pay_line)
    ids = [x for x in [
        f.get("contract_id") and f"Contract {f['contract_id']}",
        f.get("vendor_id") and f"Vendor {f['vendor_id']}",
    ] if x]
    if ids:
        lines.append(" | ".join(ids))
    if lines:
        d["job_description"] = "\n".join(lines)

    return d


def ghl_payload(em: InboundEmail) -> dict:
    """The exact JSON we POST to GHL for a parsed email — the raw extracted fields, plus
    flattened/derived scalars for GHL's webhook mapper, plus email metadata. Built here so
    the relay handler and the log API show identical shapes."""
    fields = em.fields or {}
    return {
        **fields,
        **_derived_fields(fields),
        "source": em.source,
        "job_id": em.job_id,
        "subject": em.subject,
        "from": em.from_addr,
        "message_id": em.message_id,
        "received_at": em.received_at.isoformat() if em.received_at else None,
    }


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

"""Inbound SMS ingestion.

An inbound message webhook carries From/To/Body but is not a call. We attribute it to
the tracking Number (and its campaign) the same way calls are, then upsert keyed on the
provider message SID for idempotency (retries are safe). Relay to GHL happens later, as a
durable queue job.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Caller, ContactThreadState, Message, Number, SmsOptOut
from app.providers.base import NormalizedMessageEvent
from app.services import sms
from app.services.ingestion import _get_or_create_caller, _get_or_create_provider

logger = logging.getLogger("ingestion")


async def is_contact_blocked(db: AsyncSession, contact_e164: str) -> bool:
    """True if the contact with this E.164 number is blocked in the Inbox (a right-click
    Block set contact_thread_state.blocked_at). Gates outbound send AND outbound call, so a
    blocked party can never be contacted from OWEN. Inbound stays store-but-hide."""
    if not contact_e164:
        return False
    blocked_at = (
        await db.execute(
            select(ContactThreadState.blocked_at)
            .join(Caller, ContactThreadState.caller_id == Caller.id)
            .where(Caller.phone_number == contact_e164)
        )
    ).scalar_one_or_none()
    return blocked_at is not None


async def ingest_message_event(
    db: AsyncSession, provider_name: str, evt: NormalizedMessageEvent
) -> Message:
    now = datetime.now(timezone.utc)
    provider = await _get_or_create_provider(db, provider_name)

    number = None
    if evt.to_number:
        number = (
            await db.execute(
                select(Number).where(
                    Number.provider_id == provider.id, Number.phone_number == evt.to_number
                )
            )
        ).scalar_one_or_none()
        if number is None:
            logger.warning(
                "ingest_message_event: no registered Number for to=%s (provider=%s, sid=%s) "
                "— message will have no campaign attribution",
                evt.to_number, provider_name, evt.provider_message_sid,
            )

    caller = None
    if evt.from_number:
        caller = await _get_or_create_caller(db, evt.from_number, now)

    campaign_id = number.campaign_id if number else None

    await db.execute(
        pg_insert(Message)
        .values(
            provider_id=provider.id,
            provider_message_sid=evt.provider_message_sid,
            number_id=number.id if number else None,
            caller_id=caller.id if caller else None,
            campaign_id=campaign_id,
            direction=evt.direction,
            from_number=evt.from_number,
            to_number=evt.to_number,
            body=evt.body,
            status=evt.status,
            num_media=evt.num_media,
            media_urls=evt.media_urls or None,
            raw_payload=evt.raw,
        )
        # Idempotent on the provider SID; a retry refreshes status/body without
        # regressing the relay flag (relayed_to_ghl/relayed_at left untouched).
        .on_conflict_do_update(
            index_elements=["provider_message_sid"],
            set_={"status": evt.status, "body": evt.body},
        )
    )
    await db.commit()
    row = (
        await db.execute(
            select(Message).where(
                Message.provider_message_sid == evt.provider_message_sid
            )
        )
    ).scalar_one()
    logger.info(
        "ingest_message_event: message_id=%s sid=%s number=%s campaign_id=%s from=%s",
        row.id, evt.provider_message_sid,
        number.friendly_name if number else None, campaign_id, evt.from_number,
    )
    return row


# --- Opt-out maintenance + outbound send (Ticket 10) --------------------------------------


async def get_optout_state(db: AsyncSession, number_id, contact: str) -> str | None:
    """Current opt-out state for a (number_id, contact) pair, or None if no row exists."""
    row = (
        await db.execute(
            select(SmsOptOut).where(
                SmsOptOut.number_id == number_id, SmsOptOut.contact == contact
            )
        )
    ).scalar_one_or_none()
    return row.state if row else None


async def apply_inbound_keyword(
    db: AsyncSession, number_id, contact: str, body: str | None
) -> str | None:
    """Maintain opt-out state from an inbound message body. Returns the classified keyword
    ('stop' | 'start' | 'help') or None. STOP/START upsert the row; HELP and non-keywords do
    NOT change state. No-op when number_id or contact is missing (unattributed inbound)."""
    keyword = sms.classify_keyword(body)
    if keyword is None or keyword == "help" or number_id is None or not contact:
        return keyword

    new_state = sms.next_optout_state(None, keyword)  # 'stop'->opted_out, 'start'->opted_in
    now = datetime.now(timezone.utc)
    await db.execute(
        pg_insert(SmsOptOut)
        .values(
            number_id=number_id, contact=contact, state=new_state,
            last_keyword=keyword, created_at=now, updated_at=now,
        )
        .on_conflict_do_update(
            constraint="uq_optout_number_contact",
            set_={"state": new_state, "last_keyword": keyword, "updated_at": now},
        )
    )
    await db.commit()
    logger.info("sms opt-out: number=%s contact=%s -> %s (%s)",
                number_id, contact, new_state, keyword)
    return keyword


async def enqueue_outbound_message(
    db: AsyncSession, number: Number, contact: str, body: str, user_id
) -> Message:
    """Write an outbound `messages` row (direction='outbound', sent_by_user_id) with a
    synthesized SID and return it. Does NOT send — the caller enqueues a `message_send` job.
    Gate + opt-out checks are the caller's responsibility (see api/messages.send)."""
    now = datetime.now(timezone.utc)
    caller = await _get_or_create_caller(db, contact, now)
    sid = f"owenout-{uuid.uuid4().hex}"  # replaced with the BulkVS RefId once actually sent
    msg = Message(
        provider_id=number.provider_id,
        provider_message_sid=sid,
        number_id=number.id,
        caller_id=caller.id,
        campaign_id=number.campaign_id,
        direction="outbound",
        from_number=number.phone_number,
        to_number=contact,
        body=body,
        status="queued",
        num_media=0,
        sent_by_user_id=user_id,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    logger.info("enqueue_outbound_message: id=%s number=%s to=%s by=%s",
                msg.id, number.phone_number, contact, user_id)
    return msg

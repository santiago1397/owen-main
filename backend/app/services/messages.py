"""Inbound SMS ingestion.

An inbound message webhook carries From/To/Body but is not a call. We attribute it to
the tracking Number (and its campaign) the same way calls are, then upsert keyed on the
provider message SID for idempotency (retries are safe). Relay to GHL happens later, as a
durable queue job.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Message, Number
from app.providers.base import NormalizedMessageEvent
from app.services.ingestion import _get_or_create_caller, _get_or_create_provider

logger = logging.getLogger("ingestion")


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

"""Idempotent, race-safe ingestion of normalized call events.

Correctness rules (ARCHITECTURE.md #6):
- Upsert `calls` on (provider_id, provider_call_sid) via ON CONFLICT — retries are safe.
- Advance status only when the new event outranks the stored one (WHERE new_rank > status_rank)
  — done as a single atomic UPDATE so out-of-order/duplicate events can't regress a call.
- `call_events` deduped on a natural key with ON CONFLICT DO NOTHING.
- `is_new_for_campaign` computed once, at first sight of the call.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Call, CallEvent, Caller, Number, Provider
from app.providers.base import NormalizedCallEvent

logger = logging.getLogger("ingestion")


async def _get_or_create_provider(db: AsyncSession, name: str) -> Provider:
    row = (await db.execute(select(Provider).where(Provider.name == name))).scalar_one_or_none()
    if row:
        return row
    stmt = (
        pg_insert(Provider)
        .values(name=name)
        .on_conflict_do_nothing(index_elements=["name"])
        .returning(Provider.id)
    )
    await db.execute(stmt)
    return (await db.execute(select(Provider).where(Provider.name == name))).scalar_one()


async def _get_or_create_caller(db: AsyncSession, phone: str, seen_at: datetime) -> Caller:
    stmt = (
        pg_insert(Caller)
        .values(phone_number=phone, first_seen_at=seen_at, last_seen_at=seen_at, total_calls=0)
        .on_conflict_do_update(
            index_elements=["phone_number"],
            set_={"last_seen_at": seen_at},
        )
        .returning(Caller.id)
    )
    await db.execute(stmt)
    return (await db.execute(select(Caller).where(Caller.phone_number == phone))).scalar_one()


async def ingest_status_event(
    db: AsyncSession, provider_name: str, evt: NormalizedCallEvent
) -> None:
    now = datetime.now(timezone.utc)
    seen_at = evt.started_at or now

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
                "ingest_status_event: no registered Number for to=%s (provider=%s, call_sid=%s) "
                "— call will have no campaign attribution",
                evt.to_number, provider_name, evt.provider_call_sid,
            )

    caller = None
    if evt.from_number:
        caller = await _get_or_create_caller(db, evt.from_number, seen_at)

    campaign_id = number.campaign_id if number else None

    # Per-campaign new-caller flag, computed only at first insert of this call.
    is_new_for_campaign = None
    if caller and campaign_id:
        prior = (
            await db.execute(
                select(Call.id)
                .where(Call.caller_id == caller.id, Call.campaign_id == campaign_id)
                .limit(1)
            )
        ).first()
        is_new_for_campaign = prior is None

    # Insert-or-nothing the call row (idempotent on provider_call_sid).
    insert_call = (
        pg_insert(Call)
        .values(
            provider_id=provider.id,
            provider_call_sid=evt.provider_call_sid,
            number_id=number.id if number else None,
            caller_id=caller.id if caller else None,
            campaign_id=campaign_id,
            direction=evt.direction,
            status=evt.status,
            status_rank=evt.status_rank,
            # Webhook status callbacks carry no start time; stamp first-sighting so
            # time-range dashboards include the call. Reconciliation later overwrites
            # this with the provider's authoritative start_time.
            started_at=evt.started_at or now,
            answered_at=evt.answered_at,
            ended_at=evt.ended_at,
            duration_seconds=evt.duration_seconds,
            forwarded_to=evt.forwarded_to,
            is_new_for_campaign=is_new_for_campaign,
            raw_payload=evt.raw,
        )
        .on_conflict_do_nothing(index_elements=["provider_id", "provider_call_sid"])
    )
    await db.execute(insert_call)

    # Atomic forward-only status advance. Never regress on a late/duplicate event.
    #
    # Also (re)fills number_id/caller_id/campaign_id/direction here — not just on
    # first insert. A recording event can create a bare stub row (services/recordings.py
    # _ensure_call, no attribution info available yet) before the status event that
    # actually knows the number/caller arrives; if this UPDATE didn't set them too,
    # that row would stay permanently unattributed even once the real status event
    # showed up, since the INSERT above is a no-op on conflict. COALESCE onto the
    # existing column so a later event with less information (e.g. no to_number)
    # can't regress previously-correct attribution.
    await db.execute(
        update(Call)
        .where(
            Call.provider_id == provider.id,
            Call.provider_call_sid == evt.provider_call_sid,
            Call.status_rank < evt.status_rank,
        )
        .values(
            status=evt.status,
            status_rank=evt.status_rank,
            answered_at=evt.answered_at,
            ended_at=evt.ended_at,
            duration_seconds=evt.duration_seconds,
            number_id=(number.id if number else None) or Call.number_id,
            caller_id=(caller.id if caller else None) or Call.caller_id,
            campaign_id=campaign_id or Call.campaign_id,
            direction=evt.direction or Call.direction,
        )
    )

    # Attribution back-fill, decoupled from the status-rank gate above. A call can reach
    # its terminal status via an event that lacked the tracking number — e.g. a SignalWire
    # Call-Flow-Builder *forward leg* whose to_number is the operator line, not the dialed
    # tracking number — which leaves it permanently unattributed, because the status-advance
    # UPDATE won't fire again once status_rank is maxed. When a later event (typically the
    # reconciler's authoritative inbound leg) does carry a registered Number, heal the gap.
    # Guarded on number_id IS NULL so it only ever fills a hole, never overwrites existing
    # (correct) attribution; and only runs when this event actually resolved a Number.
    if number is not None:
        await db.execute(
            update(Call)
            .where(
                Call.provider_id == provider.id,
                Call.provider_call_sid == evt.provider_call_sid,
                Call.number_id.is_(None),
            )
            .values(
                number_id=number.id,
                campaign_id=campaign_id or Call.campaign_id,
                is_new_for_campaign=(
                    is_new_for_campaign if is_new_for_campaign is not None
                    else Call.is_new_for_campaign
                ),
            )
        )

    call = (
        await db.execute(
            select(Call).where(
                Call.provider_id == provider.id,
                Call.provider_call_sid == evt.provider_call_sid,
            )
        )
    ).scalar_one()

    # Append-only event; dedup on the natural key. Only bump the counter when a
    # genuinely new event row was inserted (avoids double-counting on retries).
    inserted = (
        await db.execute(
            pg_insert(CallEvent)
            .values(
                call_id=call.id,
                event_type=evt.event_type,
                provider_sequence=evt.provider_sequence,
                payload=evt.raw,
            )
            .on_conflict_do_nothing(
                index_elements=["call_id", "event_type", "provider_sequence"]
            )
            .returning(CallEvent.id)
        )
    ).first()

    if inserted and caller and evt.status_rank >= 1:
        # Count the call once, on its first observed event.
        first_event_for_call = (
            await db.execute(select(CallEvent.id).where(CallEvent.call_id == call.id).limit(2))
        ).all()
        if len(first_event_for_call) == 1:
            await db.execute(
                update(Caller)
                .where(Caller.id == caller.id)
                .values(total_calls=Caller.total_calls + 1, last_seen_at=seen_at)
            )

    await db.commit()
    logger.info(
        "ingest_status_event: call_id=%s call_sid=%s number=%s campaign_id=%s status=%s "
        "new_event_row=%s",
        call.id, evt.provider_call_sid, number.friendly_name if number else None,
        campaign_id, evt.status, bool(inserted),
    )

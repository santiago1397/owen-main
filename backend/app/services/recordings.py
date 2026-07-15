"""Recording ingestion (Phase 2).

A recording status callback carries a CallSid but not the call's From/To/status, and
can arrive before the status webhook. So we ensure a bare `calls` row exists (the later
status event fills it in — same event-sourced row), then upsert the recording keyed on
its provider SID for idempotency.
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Call, Recording
from app.providers.base import NormalizedRecordingEvent
from app.services.ingestion import _get_or_create_provider


async def _ensure_call(db: AsyncSession, provider_id: int, call_sid: str) -> Call:
    await db.execute(
        pg_insert(Call)
        .values(provider_id=provider_id, provider_call_sid=call_sid, status_rank=0)
        .on_conflict_do_nothing(index_elements=["provider_id", "provider_call_sid"])
    )
    return (
        await db.execute(
            select(Call).where(
                Call.provider_id == provider_id, Call.provider_call_sid == call_sid
            )
        )
    ).scalar_one()


async def ingest_recording_event(
    db: AsyncSession, provider_name: str, rec: NormalizedRecordingEvent
) -> Recording:
    provider = await _get_or_create_provider(db, provider_name)
    call = await _ensure_call(db, provider.id, rec.provider_call_sid)

    await db.execute(
        pg_insert(Recording)
        .values(
            call_id=call.id,
            provider_recording_sid=rec.provider_recording_sid,
            status=rec.status,
            duration_seconds=rec.duration_seconds,
            provider_url=rec.provider_url,
        )
        .on_conflict_do_update(
            index_elements=["provider_recording_sid"],
            set_={
                "status": rec.status,
                "duration_seconds": rec.duration_seconds,
                "provider_url": rec.provider_url,
            },
        )
    )
    await db.commit()
    return (
        await db.execute(
            select(Recording).where(
                Recording.provider_recording_sid == rec.provider_recording_sid
            )
        )
    ).scalar_one()

"""Job handlers dispatched by the worker loop, keyed on Job.type.

Pipeline: recording_fetch -> transcribe -> analyze. Each stage enqueues the next only
on success, so a failure retries just that stage (queue.fail backoff) without redoing
earlier work.
"""

import logging
import os
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis.classification import get_analysis_engine
from app.analysis.transcription import get_transcription_engine
from app.core.config import settings
from app.models import Call, CallAnalysis, Caller, Message, Number, Recording, Transcription
from app.providers import ghl_client, signalwire_client, twilio_client
from app.services import queue

logger = logging.getLogger("worker.handlers")

# Per-provider media download auth (ARCHITECTURE.md #12 — never shared across providers).
_PROVIDER_AUTH = {
    "twilio": lambda: (settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
    "signalwire": lambda: (settings.SIGNALWIRE_PROJECT_ID, settings.SIGNALWIRE_AUTH_TOKEN),
}

# Per-provider remote-delete (ARCHITECTURE.md #12). Called only after the local copy is
# safely on disk, so provider-side storage is never billed. Kept per-provider, never shared.
_PROVIDER_DELETE = {
    "twilio": twilio_client.delete_recording,
    "signalwire": signalwire_client.delete_recording,
}


async def handle_recording_fetch(db: AsyncSession, payload: dict) -> None:
    """Download the provider's recording into our own storage (local disk). Idempotent."""
    recording_id = payload.get("recording_id")
    rec = await db.get(Recording, uuid.UUID(recording_id)) if recording_id else None
    if rec is None:
        logger.warning("recording_fetch: recording %s not found", recording_id)
        return
    if not (rec.storage_path and os.path.exists(rec.storage_path)):
        url = payload.get("provider_url") or rec.provider_url
        if not url:
            raise ValueError(f"recording {recording_id} has no provider_url")
        media_url = url if url.endswith(".mp3") else f"{url}.mp3"
        auth = _PROVIDER_AUTH.get(payload.get("provider", "twilio"), lambda: None)()
        os.makedirs(settings.RECORDINGS_DIR, exist_ok=True)
        dest = os.path.join(settings.RECORDINGS_DIR, f"{rec.provider_recording_sid}.mp3")
        tmp = f"{dest}.part"
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            async with client.stream("GET", media_url, auth=auth) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as fh:
                    async for chunk in resp.aiter_bytes():
                        fh.write(chunk)
        os.replace(tmp, dest)  # atomic
        rec.storage_path = dest
        rec.status = "completed"
        rec.downloaded_at = datetime.now(timezone.utc)
        await db.commit()
        logger.info("recording_fetch: stored %s", rec.provider_recording_sid)

        # Local copy is safe on disk — now drop the provider's copy so we're never
        # billed for their storage (the cheap-storage strategy). Best-effort: a failed
        # delete must not fail the pipeline; the transcript/local audio are already kept.
        if settings.DELETE_REMOTE_RECORDING:
            deleter = _PROVIDER_DELETE.get(payload.get("provider", "twilio"))
            if deleter and rec.provider_recording_sid:
                try:
                    await deleter(rec.provider_recording_sid)
                    logger.info("recording_fetch: deleted remote %s", rec.provider_recording_sid)
                except Exception as exc:  # noqa: BLE001 - non-fatal; retry happens next poll
                    logger.warning("recording_fetch: remote delete failed for %s: %s",
                                   rec.provider_recording_sid, exc)

    # Next stage: transcribe (unless already done).
    if not rec.transcribed:
        await queue.enqueue(db, "transcribe", {"recording_id": str(rec.id)})


async def handle_transcribe(db: AsyncSession, payload: dict) -> None:
    rec = await db.get(Recording, uuid.UUID(payload["recording_id"]))
    if rec is None:
        logger.warning("transcribe: recording %s not found", payload.get("recording_id"))
        return
    if not (rec.storage_path and os.path.exists(rec.storage_path)):
        raise RuntimeError(f"transcribe: audio missing for {rec.provider_recording_sid}")

    engine = get_transcription_engine()
    result = await engine.transcribe(rec.storage_path)

    db.add(Transcription(
        call_id=rec.call_id, recording_id=rec.id, engine=engine.name,
        text=result.text, language=result.language, confidence=result.confidence,
        words=result.words, status="completed",
    ))
    rec.transcribed = True  # this now permits retention to delete the audio; transcript kept
    await db.commit()
    logger.info("transcribe: %s via %s (%d chars)", rec.provider_recording_sid, engine.name,
                len(result.text or ""))

    await queue.enqueue(db, "analyze", {"call_id": str(rec.call_id)})


async def handle_analyze(db: AsyncSession, payload: dict) -> None:
    call_id = uuid.UUID(payload["call_id"])
    text = (
        await db.execute(
            select(Transcription.text).where(Transcription.call_id == call_id)
            .order_by(Transcription.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if not text:
        raise RuntimeError(f"analyze: no transcript for call {call_id}")

    engine = get_analysis_engine()
    r = await engine.analyze(text)

    await db.execute(
        pg_insert(CallAnalysis)
        .values(call_id=call_id, is_spam=r.is_spam, spam_confidence=r.spam_confidence,
                category=r.category, tags=r.tags, summary=r.summary, model=r.model)
        .on_conflict_do_update(
            index_elements=["call_id"],
            set_={"is_spam": r.is_spam, "spam_confidence": r.spam_confidence,
                  "category": r.category, "tags": r.tags, "summary": r.summary,
                  "model": r.model, "analyzed_at": datetime.now(timezone.utc)},
        )
    )
    # Surface the latest spam signal on the caller (manual `label` still overrides).
    call = await db.get(Call, call_id)
    if call and call.caller_id:
        caller = await db.get(Caller, call.caller_id)
        if caller:
            caller.spam_score = r.spam_confidence
            caller.spam_checked_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info("analyze: call %s -> spam=%s category=%s", call_id, r.is_spam, r.category)


async def handle_message_relay_ghl(db: AsyncSession, payload: dict) -> None:
    """Relay an inbound SMS to GoHighLevel via its inbound webhook. Idempotent: the
    relayed_to_ghl flag guards against re-sending on retry. Raises on failure so the
    queue retries (backoff)."""
    msg = await db.get(Message, uuid.UUID(payload["message_id"]))
    if msg is None:
        logger.warning("message_relay_ghl: message %s not found", payload.get("message_id"))
        return
    if msg.relayed_to_ghl:
        logger.info("message_relay_ghl: %s already relayed, skipping", msg.provider_message_sid)
        return

    number = await db.get(Number, msg.number_id) if msg.number_id else None
    await ghl_client.post_inbound_message({
        "message_sid": msg.provider_message_sid,
        "from": msg.from_number,
        "to": msg.to_number,
        "body": msg.body,
        "direction": msg.direction,
        "num_media": msg.num_media,
        "media_urls": msg.media_urls or [],
        "number_label": number.friendly_name if number else None,
        "received_at": msg.received_at.isoformat() if msg.received_at else None,
    })
    msg.relayed_to_ghl = True
    msg.relayed_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info("message_relay_ghl: relayed %s to GHL", msg.provider_message_sid)


HANDLERS = {
    "recording_fetch": handle_recording_fetch,
    "transcribe": handle_transcribe,
    "analyze": handle_analyze,
    "message_relay_ghl": handle_message_relay_ghl,
}

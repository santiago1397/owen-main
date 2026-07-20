"""Job handlers dispatched by the worker loop, keyed on Job.type.

Pipeline: recording_fetch -> transcribe -> analyze. Each stage enqueues the next only
on success, so a failure retries just that stage (queue.fail backoff) without redoing
earlier work.
"""

import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.analysis import audio
from app.analysis.classification import get_analysis_engine
from app.analysis.transcription import Transcript, get_transcription_engine
from app.core.config import settings
from app.models import (
    Call,
    CallAnalysis,
    Caller,
    Campaign,
    Message,
    Number,
    Provider,
    Recording,
    Transcription,
)
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

    # Next stage: transcribe (unless already done, or the caller asked to skip it —
    # e.g. a raw historical backfill that only wants the audio mirrored locally, no
    # transcription/analysis cost. Leaving transcribed=False also means retention never
    # prunes the file, since the sweep only deletes transcribed recordings).
    if not rec.transcribed and not payload.get("skip_transcribe"):
        await queue.enqueue(db, "transcribe", {"recording_id": str(rec.id)})


async def _transcribe_stereo(engine, audio_path: str) -> tuple[str, list] | None:
    """Split a 2-channel recording into per-leg mono tracks, transcribe each, and merge
    into a speaker-labeled transcript. Returns (flat_text, segments), or None to signal
    "not stereo / couldn't split — use the mono path" (probe/split failure degrades
    gracefully rather than losing the transcript, per the agreed failure policy).

    Once split, a per-channel transcription failure is *not* swallowed: it propagates so
    the queue retries the whole job (both channels), which is the correct move for a
    transient STT error."""
    if not settings.STEREO_TRANSCRIPTION_ENABLED:
        return None
    try:
        channels = await audio.probe_channel_count(audio_path)
    except Exception as exc:  # noqa: BLE001 - probe failure -> mono fallback, never fatal
        logger.warning("transcribe: ffprobe failed for %s, using mono path: %s", audio_path, exc)
        return None
    if channels < 2:
        return None

    tmpdir = tempfile.mkdtemp(prefix="stereo_")
    try:
        ch0 = os.path.join(tmpdir, "ch0.mp3")
        ch1 = os.path.join(tmpdir, "ch1.mp3")
        try:
            await audio.split_stereo(audio_path, ch0, ch1)
        except Exception as exc:  # noqa: BLE001 - split failure -> mono fallback
            logger.warning("transcribe: ffmpeg split failed for %s, using mono path: %s",
                           audio_path, exc)
            return None
        # Past this point failures propagate (raise -> queue retry).
        caller_idx = settings.STEREO_CALLER_CHANNEL
        ch_by_index = {0: ch0, 1: ch1}
        caller_res = await engine.transcribe_segmented(ch_by_index[caller_idx])
        operator_res = await engine.transcribe_segmented(ch_by_index[1 - caller_idx])
        text, segments = audio.merge_channels(
            caller_res.segments or [], operator_res.segments or []
        )
        # Both legs empty (silent call, or everything filtered as hallucination) — fall back
        # to the mono path so the whole-file model still gets a shot rather than storing an
        # empty transcript (which would then fail analyze).
        if not text.strip():
            return None
        return text, segments
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def handle_transcribe(db: AsyncSession, payload: dict) -> None:
    rec = await db.get(Recording, uuid.UUID(payload["recording_id"]))
    if rec is None:
        logger.warning("transcribe: recording %s not found", payload.get("recording_id"))
        return
    if not (rec.storage_path and os.path.exists(rec.storage_path)):
        raise RuntimeError(f"transcribe: audio missing for {rec.provider_recording_sid}")

    engine = get_transcription_engine()

    stereo = await _transcribe_stereo(engine, rec.storage_path)
    if stereo is not None:
        # Merged two-channel transcript: labeled text + structured segments. Metadata is
        # not meaningful across two sources, so it's left null (only `segments` is new).
        text, segments = stereo
        language, confidence, words = "en", None, None
    else:
        # Mono path — byte-identical to prior behavior: single unlabeled transcript, the
        # engine's own metadata, and no segments.
        result: Transcript = await engine.transcribe(rec.storage_path)
        text, segments = result.text, None
        language, confidence, words = result.language, result.confidence, result.words

    db.add(Transcription(
        call_id=rec.call_id, recording_id=rec.id, engine=engine.name,
        text=text, language=language, confidence=confidence, words=words,
        segments=segments, status="completed",
    ))
    rec.transcribed = True  # this now permits retention to delete the audio; transcript kept
    await db.commit()
    logger.info("transcribe: %s via %s (%d chars, %s)", rec.provider_recording_sid, engine.name,
                len(text or ""), f"{len(segments)} segments" if segments else "mono")

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


_MISSED_STATUSES = {"no-answer", "busy", "failed", "canceled"}


async def handle_call_relay_ghl(db: AsyncSession, payload: dict) -> None:
    """Relay a completed call (attribution + AI analysis) to GoHighLevel.

    Enqueued (delayed) when a call reaches a terminal status. Idempotent: the
    relayed_to_ghl flag guards against re-sending. If the call has a recording but its
    analysis pipeline hasn't finished yet, re-defer (so the payload carries spam/category/
    summary) until GHL_CALL_RELAY_MAX_WAIT_SECONDS has elapsed since the call ended, after
    which we relay what we have. Raises on POST failure so the queue retries (backoff)."""
    call = await db.get(Call, uuid.UUID(payload["call_id"]))
    if call is None:
        logger.warning("call_relay_ghl: call %s not found", payload.get("call_id"))
        return
    if call.relayed_to_ghl:
        logger.info("call_relay_ghl: call %s already relayed, skipping", call.id)
        return

    analysis = (
        await db.execute(select(CallAnalysis).where(CallAnalysis.call_id == call.id))
    ).scalar_one_or_none()
    recording = (
        await db.execute(select(Recording).where(Recording.call_id == call.id).limit(1))
    ).scalar_one_or_none()

    # Wait for the analysis pipeline if a recording exists but hasn't been analyzed yet,
    # so answered calls carry spam/category/summary. Bounded so a stuck/failed pipeline
    # (or a call that will never be analyzed) still relays eventually.
    if recording is not None and analysis is None:
        ended = call.ended_at or call.started_at
        now = datetime.now(timezone.utc)
        waited = (now - ended).total_seconds() if ended else float("inf")
        if waited < settings.GHL_CALL_RELAY_MAX_WAIT_SECONDS:
            logger.info("call_relay_ghl: call %s analysis pending, re-deferring", call.id)
            await queue.enqueue(
                db, "call_relay_ghl", {"call_id": str(call.id)},
                delay_seconds=settings.GHL_CALL_RELAY_DELAY_SECONDS,
            )
            return
        logger.info("call_relay_ghl: call %s waited %.0fs for analysis, relaying without it",
                    call.id, waited)

    caller = await db.get(Caller, call.caller_id) if call.caller_id else None
    number = await db.get(Number, call.number_id) if call.number_id else None
    campaign = await db.get(Campaign, call.campaign_id) if call.campaign_id else None
    provider = await db.get(Provider, call.provider_id) if call.provider_id else None
    transcript = (
        await db.execute(
            select(Transcription.text).where(Transcription.call_id == call.id)
            .order_by(Transcription.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()

    analysis_payload = None
    if analysis is not None:
        # Human overrides win over the model (ARCHITECTURE.md #5).
        analysis_payload = {
            "is_spam": analysis.is_spam_override if analysis.is_spam_override is not None
            else analysis.is_spam,
            "spam_confidence": float(analysis.spam_confidence)
            if analysis.spam_confidence is not None else None,
            "category": analysis.category_override or analysis.category,
            "tags": analysis.tags,
            "summary": analysis.summary,
            "model": analysis.model,
        }

    await ghl_client.post_call_summary({
        "call_sid": call.provider_call_sid,
        "provider": provider.name if provider else None,
        "direction": call.direction,
        "status": call.status,
        "missed": (call.status or "").lower() in _MISSED_STATUSES,
        "answered": call.answered_at is not None or (call.status or "").lower() == "completed",
        "duration_seconds": call.duration_seconds,
        "started_at": call.started_at.isoformat() if call.started_at else None,
        "answered_at": call.answered_at.isoformat() if call.answered_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        "from": caller.phone_number if caller else None,
        "to": number.phone_number if number else None,
        "number_label": number.friendly_name if number else None,
        "campaign": campaign.name if campaign else None,
        "campaign_source": campaign.source if campaign else None,
        "is_new_for_campaign": call.is_new_for_campaign,
        "caller": {
            "phone": caller.phone_number,
            "first_seen_at": caller.first_seen_at.isoformat() if caller.first_seen_at else None,
            "total_calls": caller.total_calls,
            "label": caller.label,
            "spam_score": float(caller.spam_score) if caller.spam_score is not None else None,
        } if caller else None,
        "has_recording": recording is not None,
        "analysis": analysis_payload,
        "transcript": transcript,
    })
    call.relayed_to_ghl = True
    call.relayed_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info("call_relay_ghl: relayed call %s to GHL (analysis=%s)",
                call.id, analysis_payload is not None)


HANDLERS = {
    "recording_fetch": handle_recording_fetch,
    "transcribe": handle_transcribe,
    "analyze": handle_analyze,
    "message_relay_ghl": handle_message_relay_ghl,
    "call_relay_ghl": handle_call_relay_ghl,
}

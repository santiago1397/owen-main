"""ARI-WebSocket consumer — the Asterisk ingestion mechanism (ticket 04).

Runs as ONE long-lived task inside the single-replica worker (ARCHITECTURE.md #8), only
when ASTERISK_ENABLED. It connects to the ARI events WebSocket, subscribed to our Stasis
app, and feeds each channel event through the SAME `ingest_status_event` path the Twilio
and SignalWire webhooks use — no parallel pipeline, no new DB tables.

There is deliberately NO webhook route for Asterisk: events arrive over an authenticated
localhost WS (creds in the query string), so the adapter's `verify_signature` is a no-op.

Scope: call-status ingestion only. Recording reuse + CDR reconciliation are ticket 05.

The event-routing logic (map + entry-channel ranking + dedup) lives in the pure,
synchronous `AsteriskEventRouter` in app/providers/asterisk.py, so it is unit-testable
without a live Asterisk, a WS, or a DB — see tests/test_asterisk_ingestion.py.
"""

import asyncio
import json
import logging

from app.core.config import settings
from app.db import SessionLocal
from app.flows import dtmf
from app.providers.asterisk import AsteriskEventRouter, is_entry_channel, is_flow_dial_leg
from app.services import queue
from app.services.ingestion import ingest_status_event
from app.services.recordings import ingest_recording_event

logger = logging.getLogger("worker.asterisk_consumer")

PROVIDER_NAME = "asterisk"

# Reconnect backoff for the WS loop (seconds): grow on repeated failure, cap so a flapping
# Asterisk doesn't hot-spin. Reset to the floor on a clean connect.
_BACKOFF_MIN = 1.0
_BACKOFF_MAX = 30.0


def enabled() -> bool:
    return settings.ASTERISK_ENABLED


async def _handle(router: AsteriskEventRouter, raw: str | bytes) -> None:
    try:
        event = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("asterisk_consumer: dropping non-JSON frame")
        return
    if not isinstance(event, dict):
        return

    # Ticket 15: feed the per-channel correlation registries (app/flows/dtmf) BEFORE any
    # routing. DTMF digits go to the flow run awaiting them (read_digit); channel lifecycle
    # events fan out to any dial-in-progress watcher. Both pushes are non-blocking no-ops
    # when nothing is registered, so this costs a dict lookup on the hot path.
    etype = event.get("type")
    ch = event.get("channel") if isinstance(event.get("channel"), dict) else {}
    channel_id = str(ch.get("id") or "")
    if etype == "ChannelDtmfReceived":
        dtmf.push_digit(channel_id, str(event.get("digit") or ""))
        return  # DTMF never maps onto the status vocabulary; nothing else to do
    if etype in dtmf.CHANNEL_EVENT_TYPES and channel_id:
        dtmf.push_channel_event(channel_id, event)
        # fall through — these same events also feed status ingestion below

    # Ticket 18: PlaybackFinished / RecordingFinished feed the completion-signal registries the
    # unassigned-number default handler + voicemail node await on (consent-before-ring, greeting-
    # before-beep, silence-triggered voicemail end). PlaybackFinished carries no call status, so
    # it is fully handled here; RecordingFinished falls through to the existing pipeline routing.
    if etype == "PlaybackFinished":
        pb = event.get("playback") if isinstance(event.get("playback"), dict) else {}
        dtmf.push_playback(str(pb.get("id") or ""), event)
        return
    if etype == "RecordingFinished":
        rec_obj = event.get("recording") if isinstance(event.get("recording"), dict) else {}
        dtmf.push_recording(str(rec_obj.get("name") or ""), event)
        # fall through — route_recording below still registers the row + enqueues the fetch

    # Ticket 05: a RecordingFinished routes into the EXISTING recordings pipeline — register
    # the row (idempotent on the recording SID) and enqueue a fetch (a local spool move for
    # Asterisk) which chains into transcribe -> analyze, exactly like a Twilio recording.
    rec = router.route_recording(event)
    if rec is not None:
        async with SessionLocal() as db:
            row = await ingest_recording_event(db, PROVIDER_NAME, rec)
            if row.storage_path is None:
                await queue.enqueue(db, "recording_fetch", {
                    "provider": PROVIDER_NAME,
                    "recording_id": str(row.id),
                    "recording_sid": rec.provider_recording_sid,
                    "provider_url": rec.provider_url,
                })
        logger.info("asterisk_consumer: recording %s linkedid=%s status=%s",
                    rec.provider_recording_sid, rec.provider_call_sid, rec.status)
        return

    evt = router.route(event)
    if evt is None:
        return
    async with SessionLocal() as db:
        await ingest_status_event(db, PROVIDER_NAME, evt)
    logger.info("asterisk_consumer: ingested %s linkedid=%s status=%s",
                event.get("type"), evt.provider_call_sid, evt.status)

    # Ticket 07: on the entry channel's (freshly-routed) StasisStart, hand the call to the
    # flow interpreter. Fire-and-forget so the WS read loop keeps draining events while the
    # (possibly minutes-long) flow runs. Only reached with ASTERISK_ENABLED on. A flow-dial
    # outbound leg (Ticket 15.3) also StasisStarts into our app — it must NEVER start a
    # second flow run (its interpreter is already driving it).
    if event.get("type") == "StasisStart" and is_entry_channel(event) and not is_flow_dial_leg(event):
        asyncio.create_task(_run_flow(event))


async def _run_flow(event: dict) -> None:
    """Best-effort flow-interpreter handoff; a failure here must never kill the consumer.
    Imports are lazy so the interpreter's DB/httpx deps aren't pulled in at module load.

    Registers the entry channel's DTMF queue for the duration of the run (Ticket 15.1) so
    `read_digit` can await ChannelDtmfReceived events; the finally guarantees the queue is
    unregistered however the flow ends — the registry never leaks."""
    ch = event.get("channel") if isinstance(event.get("channel"), dict) else {}
    channel_id = str(ch.get("id") or "")
    if channel_id:
        dtmf.register_digits(channel_id)
    try:
        from app.flows.runtime import run_flow_for_stasis
        from app.providers.asterisk_client import AsteriskAriClient

        await run_flow_for_stasis(event, AsteriskAriClient())
    except Exception:  # noqa: BLE001 - flow failures are isolated from ingestion
        logger.exception("asterisk_consumer: flow interpreter failed")
    finally:
        if channel_id:
            dtmf.unregister_digits(channel_id)


async def run_consumer() -> None:
    """Connect to the ARI events WebSocket and stream events forever, reconnecting with
    backoff on any drop. Gated by the caller on `enabled()`; safe to run as a bare task."""
    # Imported lazily so the module stays importable (and unit-testable) even where the
    # `websockets` package isn't installed.
    import websockets

    router = AsteriskEventRouter()
    backoff = _BACKOFF_MIN
    url = settings.ari_ws_url
    logger.info("asterisk_consumer: starting, app=%s", settings.ARI_APP)
    while True:
        try:
            async with websockets.connect(url) as ws:
                logger.info("asterisk_consumer: connected to ARI events WS")
                backoff = _BACKOFF_MIN
                async for raw in ws:
                    try:
                        await _handle(router, raw)
                    except Exception:  # noqa: BLE001 - one bad event must not kill the loop
                        logger.exception("asterisk_consumer: failed to handle event")
        except asyncio.CancelledError:
            logger.info("asterisk_consumer: cancelled, shutting down")
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect on any WS/connect failure
            logger.warning("asterisk_consumer: WS error (%s); reconnecting in %.0fs",
                           exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

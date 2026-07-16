"""Reconciliation: pull each provider's REST API for a recent window and upsert
anything webhooks missed (ARCHITECTURE.md #6). Uses the same ingest code path as the
webhooks, so backfilled calls are indistinguishable from live ones.
"""

import logging

from app.core.config import settings
from app.db import SessionLocal
from app.providers import signalwire_client, twilio_client
from app.services import queue
from app.services.ingestion import ingest_status_event
from app.services.recordings import ingest_recording_event

logger = logging.getLogger("worker.reconciler")

_SOURCES = {
    "twilio": twilio_client.fetch_recent_calls,
    # The classic Compatibility API (fetch_recent_calls) never reports calls routed
    # through Call Flow Builder — confirmed dead for this account. The modern Voice
    # API (GET /api/voice/logs) is what actually works; see SIGNALWIRE_CFB_INGESTION.md.
    "signalwire": signalwire_client.fetch_recent_calls_voice_logs,
}

# Providers whose recordings we discover by polling (Call Flow Builder does not POST
# a recordingStatusCallback to us reliably; webhooks still work too and are idempotent).
_RECORDING_SOURCES = {
    "signalwire": signalwire_client.fetch_recordings_via_voice_logs,
}


def _is_inbound(evt) -> bool:
    """Keep only the inbound leg. A forwarded call (e.g. -> ucallz) also returns an
    outbound-dial leg whose `To` is the forward target, which would never match a
    registered tracking number and would double-count every call."""
    direction = (evt.direction or "").lower()
    # Treat unknown/blank direction as inbound so we never silently drop real calls.
    return not direction or direction.startswith("inbound")


async def reconcile_recent(window_hours: int | None = None) -> int:
    hours = window_hours or settings.RECONCILE_WINDOW_HOURS
    total = 0
    for provider, fetch in _SOURCES.items():
        try:
            events = await fetch(hours)
        except Exception as exc:  # noqa: BLE001 - one provider's outage must not block others
            logger.warning("reconcile: %s call fetch failed: %s", provider, exc)
            continue
        kept = 0
        for evt in events:
            if not evt.provider_call_sid or not _is_inbound(evt):
                continue
            async with SessionLocal() as db:
                await ingest_status_event(db, provider, evt)
            kept += 1
        total += kept
        if events:
            logger.info("reconcile: %s processed %s/%s inbound calls (last %sh)",
                        provider, kept, len(events), hours)

    for provider, fetch_recordings in _RECORDING_SOURCES.items():
        try:
            recs = await fetch_recordings(hours)
        except Exception as exc:  # noqa: BLE001 - one provider's outage must not block others
            logger.warning("reconcile: %s recording fetch failed: %s", provider, exc)
            continue
        enqueued = 0
        for rec in recs:
            if not rec.provider_recording_sid:
                continue
            async with SessionLocal() as db:
                row = await ingest_recording_event(db, provider, rec)
                # Only enqueue a fetch if we don't already hold the audio locally —
                # the fetch handler is idempotent, this just avoids busywork each poll.
                if row.storage_path is None:
                    await queue.enqueue(
                        db,
                        "recording_fetch",
                        {
                            "provider": provider,
                            "recording_id": str(row.id),
                            "recording_sid": rec.provider_recording_sid,
                            "provider_url": rec.provider_url,
                        },
                    )
                    enqueued += 1
        if recs:
            logger.info("reconcile: %s found %s recordings, enqueued %s fetches (last %sh)",
                        provider, len(recs), enqueued, hours)

    return total

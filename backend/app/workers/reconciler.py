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

def _call_sources() -> dict:
    """provider name -> async fetch(window_hours) for call reconciliation.

    Each configured Twilio account is its own provider identity (its own name + creds),
    so calls attribute to the right account and the `provider` name flows into ingestion.
    The default-arg binding freezes each account into its own closure. SignalWire's classic
    Compatibility API (fetch_recent_calls) never reports Call Flow Builder calls — confirmed
    dead for this account; the modern Voice API (GET /api/voice/logs) is what actually works
    (see SIGNALWIRE_CFB_INGESTION.md)."""
    sources = {
        acct.name: (lambda h, a=acct: twilio_client.fetch_recent_calls(a, h))
        for acct in settings.twilio_accounts()
    }
    sources["signalwire"] = signalwire_client.fetch_recent_calls_voice_logs
    return sources


def _recording_sources() -> dict:
    """provider name -> async fetch(window_hours) for recording discovery via polling
    (webhooks still work too and are idempotent). SignalWire's Call Flow Builder doesn't
    POST a recordingStatusCallback reliably; Twilio's Studio "Connect Call To" widget
    exposes no callback field at all, so Studio-recorded Twilio calls only reach us here."""
    sources = {
        acct.name: (lambda h, a=acct: twilio_client.fetch_recent_recordings(a, h))
        for acct in settings.twilio_accounts()
    }
    sources["signalwire"] = signalwire_client.fetch_recordings_via_voice_logs
    return sources


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
    for provider, fetch in _call_sources().items():
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
                call = await ingest_status_event(db, provider, evt)
                # Relay backfilled terminal calls to GHL too, so webhook-missed calls
                # aren't invisible in the CRM. Same delayed, relay-once path as the
                # webhook route; the flag makes this idempotent across reconcile polls.
                if (
                    settings.GHL_CALL_WEBHOOK_URL
                    and call.status_rank >= 4
                    and not call.relayed_to_ghl
                ):
                    await queue.enqueue(
                        db,
                        "call_relay_ghl",
                        {"call_id": str(call.id)},
                        delay_seconds=settings.GHL_CALL_RELAY_DELAY_SECONDS,
                    )
            kept += 1
        total += kept
        if events:
            logger.info("reconcile: %s processed %s/%s inbound calls (last %sh)",
                        provider, kept, len(events), hours)

    for provider, fetch_recordings in _recording_sources().items():
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

"""Reconciliation: pull each provider's REST API for a recent window and upsert
anything webhooks missed (ARCHITECTURE.md #6). Uses the same ingest code path as the
webhooks, so backfilled calls are indistinguishable from live ones.
"""

import logging

from app.core.config import settings
from app.db import SessionLocal
from app.providers import signalwire_client, twilio_client
from app.services.ingestion import ingest_status_event

logger = logging.getLogger("worker.reconciler")

_SOURCES = {
    "twilio": twilio_client.fetch_recent_calls,
    "signalwire": signalwire_client.fetch_recent_calls,
}


async def reconcile_recent(window_hours: int | None = None) -> int:
    hours = window_hours or settings.RECONCILE_WINDOW_HOURS
    total = 0
    for provider, fetch in _SOURCES.items():
        events = await fetch(hours)
        for evt in events:
            if not evt.provider_call_sid:
                continue
            async with SessionLocal() as db:
                await ingest_status_event(db, provider, evt)
            total += 1
        if events:
            logger.info("reconcile: %s processed %s calls (last %sh)", provider, len(events), hours)
    return total

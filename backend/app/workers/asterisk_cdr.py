"""Asterisk CDR -> Postgres reconciler (ticket 05).

Closes the gap the live ARI-WebSocket consumer can miss: a worker restart mid-call, or the
known StasisEnd terminal-status gap (interpreter.py) where the entry channel leaves Stasis
and its terminal `ChannelDestroyed` never reaches the WS. Asterisk's own CDR engine records
every call regardless, so we read CDR rows and project them into the SAME
`call_events`->`calls` projection the WS uses — a CDR-completed call is indistinguishable
from a live-ingested one.

MECHANISM (documented in asterisk/README.md): Asterisk `cdr_pgsql` writes one CDR row per
call into a `cdr` table in the SAME Postgres database OWEN already owns (a dedicated
`asterisk` DB role with INSERT on `cdr` only). We do NOT create or own that table (it is
Asterisk's — no Alembic migration), we only READ it here. If the table is absent (Asterisk
not deployed) the scan is caught and skipped, exactly like a provider outage in reconciler.py.

IDEMPOTENCY: `cdr_row_to_event` stamps `provider_sequence = "{linkedid}:{status}"`, the SAME
dedup key the WS adapter uses, so (a) a CDR terminal event and a WS terminal event of the
same status collapse onto one `call_events` row, and (b) re-running this reconciler never
double-counts — `ingest_status_event` is forward-only on status_rank and dedups call_events
on their natural key. Gated on ASTERISK_ENABLED; off = never scheduled, never runs.
"""

import logging

from sqlalchemy import text

from app.core.config import settings
from app.db import SessionLocal
from app.providers.asterisk import cdr_row_to_event
from app.services import queue
from app.services.ingestion import ingest_status_event

logger = logging.getLogger("worker.asterisk_cdr")

PROVIDER_NAME = "asterisk"

# Columns read from the cdr table. `end` is a SQL keyword so it must be quoted. linkedid +
# answer + end require those columns to exist in the cdr schema (cdr_pgsql is adaptive and
# populates any column present) — see asterisk/README.md for the required column set.
_CDR_QUERY = text(
    """
    SELECT linkedid, uniqueid, src, dst, disposition,
           start, answer, "end", duration, billsec
    FROM cdr
    WHERE start >= now() - make_interval(hours => :hours)
    ORDER BY start
    """
)


def enabled() -> bool:
    return settings.ASTERISK_ENABLED


async def reconcile_cdr(window_hours: int | None = None) -> int:
    """Scan recent CDR rows and backfill/complete their calls in the projection. Returns the
    number of entry-leg rows ingested. Safe to run repeatedly (idempotent)."""
    hours = window_hours or settings.ASTERISK_CDR_WINDOW_HOURS

    try:
        async with SessionLocal() as db:
            rows = (await db.execute(_CDR_QUERY, {"hours": hours})).mappings().all()
    except Exception as exc:  # noqa: BLE001 - missing table / Asterisk not deployed -> skip
        logger.warning("asterisk_cdr: CDR scan failed (table absent?): %s", exc)
        return 0

    kept = 0
    for row in rows:
        evt = cdr_row_to_event(dict(row))
        if evt is None:
            continue
        async with SessionLocal() as db:
            call = await ingest_status_event(db, PROVIDER_NAME, evt)
            # Relay backfilled terminal calls to GHL too (same relay-once, delayed path as
            # the webhook/reconciler route; the flag makes it idempotent across scans).
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
    if rows:
        logger.info("asterisk_cdr: scanned %s CDR rows, ingested %s entry-leg calls (last %sh)",
                    len(rows), kept, hours)
    return kept

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
from app.providers.asterisk import AsteriskEventRouter
from app.services.ingestion import ingest_status_event

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
    evt = router.route(event)
    if evt is None:
        return
    async with SessionLocal() as db:
        await ingest_status_event(db, PROVIDER_NAME, evt)
    logger.info("asterisk_consumer: ingested %s linkedid=%s status=%s",
                event.get("type"), evt.provider_call_sid, evt.status)


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

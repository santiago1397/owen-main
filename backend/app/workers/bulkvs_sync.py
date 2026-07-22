"""BulkVS number-inventory poller (APScheduler job on the worker, Ticket 03).

Every BULKVS_SYNC_POLL_SECONDS, when the platform is enabled, pull GET /tnRecord and
mirror the operator's owned DIDs into `numbers` (add-only insert + soft-release on vanish
+ reactivate on return + one-way ReferenceID->friendly_name label mirror). There is no
inventory webhook, so this poll IS the sync. Gated on ASTERISK_ENABLED + REST creds via
settings.bulkvs_api_enabled so the platform stays dark by default.

Mirrors mail_poller: best-effort fetch (a failed poll logs and retries next tick), DB work
in a fresh session, all heavy logic in services (services.number_sync.apply_sync).
"""

import logging

from app.core.config import settings
from app.db import SessionLocal
from app.providers import bulkvs_client
from app.services.number_sync import apply_sync

logger = logging.getLogger("worker.bulkvs_sync")


def enabled() -> bool:
    return settings.bulkvs_api_enabled


async def sync_numbers() -> None:
    if not enabled():
        return
    try:
        records = await bulkvs_client.fetch_tn_records()
    except Exception:  # noqa: BLE001 - a connect/auth/HTTP failure retries next poll
        logger.exception("bulkvs_sync: /tnRecord fetch failed")
        return
    async with SessionLocal() as db:
        await apply_sync(db, records)

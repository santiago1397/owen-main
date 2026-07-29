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

    # Which DIDs arrived by PORT (free) rather than purchase ($0.05 setup) — billing needs
    # the distinction. Best-effort and separate from the inventory sync: a failure here must
    # not block mirroring the numbers themselves, so it degrades to "no port info this poll".
    ported: set[str] | None = None
    try:
        ported = await bulkvs_client.fetch_ported_numbers()
    except Exception:  # noqa: BLE001 - port info is a billing nicety, not core inventory
        logger.warning("bulkvs_sync: /portTn fetch failed; leaving ported_in unchanged")

    async with SessionLocal() as db:
        await apply_sync(db, records)
        if ported:
            await _mark_ported(db, ported)


async def _mark_ported(db, ported: set[str]) -> None:
    """Set `ported_in` on the DIDs BulkVS reports as port-ins. One-way and set-only: a number
    that was ported stays ported, so this never clears the flag (a completed port order can
    age out of the API's window without the number ceasing to have been ported)."""
    from sqlalchemy import select

    from app.models import Number

    rows = (
        await db.execute(
            select(Number).where(
                Number.phone_number.in_(sorted(ported)), Number.ported_in.is_(False)
            )
        )
    ).scalars().all()
    for row in rows:
        row.ported_in = True
    if rows:
        await db.commit()
        logger.info("bulkvs_sync: marked %d number(s) as ported-in", len(rows))

"""Worker container entrypoint (single replica — ARCHITECTURE.md #8).

Runs two things in one process:
- an async loop that drains the Postgres job queue (FOR UPDATE SKIP LOCKED)
- APScheduler for periodic jobs (reconciliation, recording retention)

Because this container is a single replica, the scheduler is a guaranteed singleton
— which is exactly why it does NOT live in the multi-worker `app` container.
"""

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import settings
from app.db import SessionLocal
from app.migrate import run_migrations
from app.services import queue
from app.workers import asterisk_consumer
from app.workers.asterisk_cdr import enabled as asterisk_cdr_enabled
from app.workers.asterisk_cdr import reconcile_cdr
from app.workers.handlers import HANDLERS
from app.workers.bulkvs_sync import enabled as bulkvs_sync_enabled
from app.workers.bulkvs_sync import sync_numbers as bulkvs_sync_numbers
from app.workers.mail_poller import enabled as mail_enabled
from app.workers.mail_poller import poll_mailbox
from app.workers.reconciler import reconcile_recent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worker")

POLL_INTERVAL = 2.0


async def drain_loop() -> None:
    while True:
        async with SessionLocal() as db:
            job = await queue.claim_one(db)
            if job is None:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            handler = HANDLERS.get(job.type)
            try:
                if handler is None:
                    raise ValueError(f"no handler for job type {job.type!r}")
                await handler(db, job.payload or {})
                await queue.complete(db, job)
            except Exception as exc:  # noqa: BLE001 - queue records the error and retries
                logger.exception("job %s failed", job.id)
                await queue.fail(db, job, str(exc))


async def retention_sweep() -> None:
    """Delete on-disk recordings older than the retention window, but ONLY once
    transcribed (ARCHITECTURE.md #9 — transcription gates deletion). The DB row and
    transcript are kept forever; only the audio file is removed and storage_path nulled.
    """
    import os
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.models import Recording

    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.RECORDING_RETENTION_DAYS)
    removed = 0
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Recording).where(
                    Recording.transcribed.is_(True),
                    Recording.storage_path.is_not(None),
                    Recording.downloaded_at < cutoff,
                )
            )
        ).scalars().all()
        for rec in rows:
            if rec.storage_path and os.path.exists(rec.storage_path):
                os.remove(rec.storage_path)
            rec.storage_path = None
            removed += 1
        await db.commit()
    logger.info("retention sweep: removed %s transcribed recordings older than %sd",
                removed, settings.RECORDING_RETENTION_DAYS)


def build_scheduler() -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    # Polling is the primary ingestion path for Call Flow Builder numbers (no per-status
    # webhooks), so keep it frequent — calls/recordings should surface within minutes.
    sched.add_job(reconcile_recent, "interval", minutes=5, id="reconcile")
    sched.add_job(retention_sweep, "interval", hours=6, id="retention")
    # Inbound-email ingestion (Hostinger IMAP). Only scheduled when a mailbox is configured
    # — otherwise it's a no-op and there's no point waking up for it.
    if mail_enabled():
        sched.add_job(
            poll_mailbox, "interval",
            seconds=settings.INBOUND_MAIL_POLL_SECONDS, id="mail_poll",
        )
        logger.info("mail poller scheduled every %ss", settings.INBOUND_MAIL_POLL_SECONDS)
    # BulkVS number-inventory sync (Ticket 03). Only when the platform flag + REST creds are
    # set — otherwise a no-op, so there's no point waking up for it.
    if bulkvs_sync_enabled():
        sched.add_job(
            bulkvs_sync_numbers, "interval",
            seconds=settings.BULKVS_SYNC_POLL_SECONDS, id="bulkvs_sync",
        )
        logger.info("bulkvs number sync scheduled every %ss", settings.BULKVS_SYNC_POLL_SECONDS)
    # Asterisk CDR -> Postgres reconcile (Ticket 05). Only when the platform flag is on —
    # otherwise a no-op, so there's no point waking up for it. Backfills/completes any call
    # the live ARI-WS consumer missed (worker restart, StasisEnd terminal-status gap).
    if asterisk_cdr_enabled():
        sched.add_job(
            reconcile_cdr, "interval",
            seconds=settings.ASTERISK_CDR_POLL_SECONDS, id="asterisk_cdr",
        )
        logger.info("asterisk CDR reconcile scheduled every %ss", settings.ASTERISK_CDR_POLL_SECONDS)
    return sched


async def main() -> None:
    run_migrations()
    scheduler = build_scheduler()
    scheduler.start()
    # Asterisk ARI-WebSocket ingestion consumer (ticket 04) — flag-gated; with
    # ASTERISK_ENABLED off no task starts and the worker behaves exactly as before.
    if asterisk_consumer.enabled():
        asyncio.create_task(asterisk_consumer.run_consumer())
        logger.info("asterisk ARI consumer task started")
    logger.info("worker started")
    await drain_loop()


if __name__ == "__main__":
    asyncio.run(main())

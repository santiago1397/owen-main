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
from app.workers.handlers import HANDLERS
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
    return sched


async def main() -> None:
    run_migrations()
    scheduler = build_scheduler()
    scheduler.start()
    logger.info("worker started")
    await drain_loop()


if __name__ == "__main__":
    asyncio.run(main())

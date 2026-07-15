"""Durable Postgres-backed job queue (ARCHITECTURE.md #7).

Enqueue from anywhere; the worker drains with FOR UPDATE SKIP LOCKED so multiple
drainers never grab the same job.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job

MAX_ATTEMPTS = 5


async def enqueue(db: AsyncSession, job_type: str, payload: dict) -> None:
    db.add(Job(type=job_type, payload=payload))
    await db.commit()


async def claim_one(db: AsyncSession) -> Job | None:
    now = datetime.now(timezone.utc)
    row = (
        await db.execute(
            text(
                """
                UPDATE jobs SET status='running', locked_at=:now, attempts=attempts+1
                WHERE id = (
                    SELECT id FROM jobs
                    WHERE status='pending' AND run_after <= :now
                    ORDER BY run_after
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id
                """
            ),
            {"now": now},
        )
    ).first()
    if not row:
        return None
    return await db.get(Job, row[0])


async def complete(db: AsyncSession, job: Job) -> None:
    job.status = "done"
    await db.commit()


async def fail(db: AsyncSession, job: Job, error: str) -> None:
    if job.attempts >= MAX_ATTEMPTS:
        job.status = "failed"
    else:
        job.status = "pending"
        job.run_after = datetime.now(timezone.utc) + timedelta(seconds=30 * job.attempts)
    job.last_error = error[:2000]
    await db.commit()

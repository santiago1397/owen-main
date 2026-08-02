"""`/api/ai/health/pipeline` — one request that answers "is anything broken right now?".

OWEN's failure modes are mostly silent: the reconciler stops finding calls, the mailbox poller
stops parsing, a job dies after five attempts, a relay never reaches GoHighLevel. None of those
raise anywhere a person would see. This endpoint turns each into a freshness number and a
count, and derives an overall verdict so a caller does not have to know which of fifteen
figures matters.

The verdict is deliberately conservative: `degraded` means "something needs a human", not
"the platform is down". Staleness thresholds are generous multiples of the relevant schedule
(the reconciler runs every 5 min, so 60 min of silence is a real signal, not jitter).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ai.deps import AuthedKey, require_scope
from app.api.ai.envelope import ok
from app.api.ai.filters import REAL_CALL
from app.core.apikeys import SCOPE_READ
from app.core.config import settings
from app.db import get_db
from app.models import Call, InboundEmail, Job, Message, Recording
from app.providers import asterisk_client
from app.services.queue import MAX_ATTEMPTS

router = APIRouter(prefix="/api/ai", tags=["ai"])

# How long a feed may go quiet before it is worth mentioning. Calls are generous because a
# genuinely quiet night is normal for a roofing business; the mailbox is tighter because it
# polls every ~90s and silence there usually means IMAP auth broke.
STALE_CALLS_MINUTES = 24 * 60
STALE_MAIL_MINUTES = 6 * 60


def _age_minutes(ts: datetime | None, now: datetime) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return round((now - ts).total_seconds() / 60.0, 1)


@router.get("/health/pipeline")
async def pipeline_health(
    db: AsyncSession = Depends(get_db),
    _: AuthedKey = Depends(require_scope(SCOPE_READ)),
) -> dict:
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)

    last_call = (await db.execute(select(func.max(Call.started_at)).where(REAL_CALL))).scalar_one()
    calls_24h = (await db.execute(
        select(func.count()).select_from(Call).where(REAL_CALL, Call.started_at >= day_ago)
    )).scalar_one()

    last_email = (await db.execute(select(func.max(InboundEmail.received_at)))).scalar_one()
    emails_24h = (await db.execute(
        select(func.count()).select_from(InboundEmail).where(InboundEmail.received_at >= day_ago)
    )).scalar_one()
    email_parse_failures_24h = (await db.execute(
        select(func.count()).select_from(InboundEmail)
        .where(InboundEmail.received_at >= day_ago, InboundEmail.parse_status == "failed")
    )).scalar_one()
    email_relay_failures = (await db.execute(
        select(func.count()).select_from(InboundEmail).where(InboundEmail.relay_status == "failed")
    )).scalar_one()

    last_message = (await db.execute(select(func.max(Message.received_at)))).scalar_one()

    job_counts = {
        s: c for s, c in (await db.execute(
            select(Job.status, func.count()).group_by(Job.status)
        )).all()
    }
    dead_jobs = (await db.execute(
        select(func.count()).select_from(Job)
        .where(Job.status == "failed", Job.attempts >= MAX_ATTEMPTS)
    )).scalar_one()
    oldest_pending = (await db.execute(
        select(func.min(Job.run_after)).where(Job.status == "pending")
    )).scalar_one()

    # A recording downloaded but never transcribed means the pipeline stalled between stages.
    stuck_recordings = (await db.execute(
        select(func.count()).select_from(Recording)
        .where(Recording.storage_path.is_not(None), Recording.transcribed.is_(False),
               Recording.downloaded_at < now - timedelta(hours=6))
    )).scalar_one()

    unrelayed_calls = (await db.execute(
        select(func.count()).select_from(Call)
        .where(REAL_CALL, Call.started_at >= day_ago, Call.relayed_to_ghl.is_(False))
    )).scalar_one()

    telephony = {"asterisk_enabled": settings.ASTERISK_ENABLED, "ari_reachable": None,
                 "trunk_registered": None}
    if settings.ASTERISK_ENABLED:
        # Best-effort, exactly like /health/telephony: a probe failure is information, not
        # a reason for this endpoint to fail.
        try:
            telephony["ari_reachable"] = await asterisk_client.ari_reachable()
            telephony["trunk_registered"] = (
                await asterisk_client.trunk_registered() if telephony["ari_reachable"] else False
            )
        except Exception:  # noqa: BLE001 - never fail the health read on a probe
            telephony["ari_reachable"] = False

    call_age = _age_minutes(last_call, now)
    mail_age = _age_minutes(last_email, now)

    problems: list[str] = []
    if dead_jobs:
        problems.append(f"{dead_jobs} job(s) dead after {MAX_ATTEMPTS} attempts")
    if email_parse_failures_24h:
        problems.append(f"{email_parse_failures_24h} email(s) failed to parse in the last 24h "
                        f"(leads are being dropped)")
    if email_relay_failures:
        problems.append(f"{email_relay_failures} email relay(s) to GoHighLevel failed")
    if stuck_recordings:
        problems.append(f"{stuck_recordings} recording(s) downloaded >6h ago but never transcribed")
    if call_age is not None and call_age > STALE_CALLS_MINUTES:
        problems.append(f"no call ingested in {round(call_age / 60)}h")
    if mail_age is not None and mail_age > STALE_MAIL_MINUTES and emails_24h == 0:
        problems.append(f"no email ingested in {round(mail_age / 60)}h")
    if settings.ASTERISK_ENABLED and telephony["ari_reachable"] is False:
        problems.append("Asterisk ARI is unreachable")

    healthy = not problems
    return ok(
        summary=("Pipeline healthy." if healthy
                 else "Pipeline DEGRADED: " + "; ".join(problems) + "."),
        data={
            "status": "healthy" if healthy else "degraded",
            "problems": problems,
            "ingestion": {
                "last_call_at": last_call.isoformat() if last_call else None,
                "minutes_since_last_call": call_age,
                "calls_last_24h": calls_24h,
                "last_message_at": last_message.isoformat() if last_message else None,
                "last_email_at": last_email.isoformat() if last_email else None,
                "minutes_since_last_email": mail_age,
                "emails_last_24h": emails_24h,
            },
            "queue": {
                "pending": job_counts.get("pending", 0),
                "running": job_counts.get("running", 0),
                "done": job_counts.get("done", 0),
                "failed": job_counts.get("failed", 0),
                "dead": dead_jobs,
                "oldest_pending_run_after": oldest_pending.isoformat() if oldest_pending else None,
            },
            "relays": {
                "email_parse_failures_24h": email_parse_failures_24h,
                "email_relay_failures_total": email_relay_failures,
                "calls_24h_not_relayed_to_ghl": unrelayed_calls,
            },
            "recordings": {"downloaded_but_never_transcribed_over_6h": stuck_recordings},
            "telephony": telephony,
        },
        notes=[
            "`degraded` means something needs a human, not that the platform is down.",
            "Call ingestion is polled every 5 minutes; a quiet night is normal for this business, "
            "so call staleness is only reported after 24h of silence.",
        ],
    )

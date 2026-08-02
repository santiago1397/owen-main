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
    # Only 'failed' counts. 'ignored' means the email was never a job notification
    # (cancellation, note on an existing job, Dispatch account mail) — expected traffic that
    # carries no lead, and counting it here is what previously made a working parser look
    # broken. See providers/dispatch_email.py.
    email_parse_failures_24h = (await db.execute(
        select(func.count()).select_from(InboundEmail)
        .where(InboundEmail.received_at >= day_ago, InboundEmail.parse_status == "failed")
    )).scalar_one()
    email_ignored_24h = (await db.execute(
        select(func.count()).select_from(InboundEmail)
        .where(InboundEmail.received_at >= day_ago, InboundEmail.parse_status == "ignored")
    )).scalar_one()
    email_relay_failures = (await db.execute(
        select(func.count()).select_from(InboundEmail).where(InboundEmail.relay_status == "failed")
    )).scalar_one()
    # The identities behind that count, because "4 relay failures" is an abstraction and
    # "Simon Kakon's roof job never reached the CRM" is a thing someone can act on.
    stranded = (await db.execute(
        select(InboundEmail.job_id, InboundEmail.fields, InboundEmail.received_at)
        .where(InboundEmail.relay_status == "failed")
        .order_by(InboundEmail.received_at.desc()).limit(25)
    )).all()

    last_message = (await db.execute(select(func.max(Message.received_at)))).scalar_one()

    job_counts = {
        s: c for s, c in (await db.execute(
            select(Job.status, func.count()).group_by(Job.status)
        )).all()
    }
    # A job that died because the PROVIDER no longer has the media is not a fault in OWEN and
    # no amount of engineering will revive it: Twilio deletes recordings on its own retention
    # schedule, and `recordings.status` is already set to 'absent' for these. Counting them as
    # dead jobs put ~388 permanent, unfixable entries in front of the ~30 that are real.
    GONE_UPSTREAM = Job.last_error.ilike("%404%")
    dead_jobs = (await db.execute(
        select(func.count()).select_from(Job)
        .where(Job.status == "failed", Job.attempts >= MAX_ATTEMPTS, ~GONE_UPSTREAM)
    )).scalar_one()
    dead_gone_upstream = (await db.execute(
        select(func.count()).select_from(Job)
        .where(Job.status == "failed", Job.attempts >= MAX_ATTEMPTS, GONE_UPSTREAM)
    )).scalar_one()
    oldest_pending = (await db.execute(
        select(func.min(Job.run_after)).where(Job.status == "pending")
    )).scalar_one()

    # A recording downloaded but never transcribed CAN mean the pipeline stalled between
    # stages — but only for recordings that went through the live pipeline. The one-off
    # historical backfill deliberately passes skip_transcribe=True (a pure audio+metadata
    # mirror, no OpenAI cost, and untranscribed audio is never pruned by retention), so it
    # leaves tens of thousands of intentionally-untranscribed files behind forever. Bounding
    # this to the last 7 days keeps a genuine stall visible within hours without a historical
    # backfill screaming permanently: 26,393 files from one July backfill were being reported
    # as a pipeline problem.
    stuck_window = now - timedelta(days=7)
    stuck_recordings = (await db.execute(
        select(func.count()).select_from(Recording)
        .where(Recording.storage_path.is_not(None), Recording.transcribed.is_(False),
               Recording.downloaded_at < now - timedelta(hours=6),
               Recording.downloaded_at >= stuck_window)
    )).scalar_one()
    untranscribed_archive = (await db.execute(
        select(func.count()).select_from(Recording)
        .where(Recording.storage_path.is_not(None), Recording.transcribed.is_(False),
               Recording.downloaded_at < stuck_window)
    )).scalar_one()

    # The completed-call relay only runs when GHL_CALL_WEBHOOK_URL is set. With it unset the
    # relay is switched OFF, so every call is "not relayed" — reporting that as a count reads
    # like a backlog of failures when nothing is failing and nothing is meant to happen.
    # (The EMAIL relay is separate and uses the direct API; only calls depend on this URL.)
    call_relay_enabled = bool(settings.GHL_CALL_WEBHOOK_URL)
    unrelayed_calls = (await db.execute(
        select(func.count()).select_from(Call)
        .where(REAL_CALL, Call.started_at >= day_ago, Call.relayed_to_ghl.is_(False))
    )).scalar_one() if call_relay_enabled else None

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

    # Two lists, because they are two different jobs for two different people.
    #
    # `problems`  — the software or its plumbing is misbehaving. An engineer fixes these.
    # `needs_attention` — the software worked correctly and a BUSINESS outcome still needs a
    #   human: a lead GoHighLevel refused, a work order whose email we could not read. Filing
    #   those under "errors" buries them among stack traces and, worse, invites the reader to
    #   dismiss them as noise. They are the most valuable thing this endpoint reports.
    problems: list[str] = []
    needs_attention: list[str] = []

    if dead_jobs:
        problems.append(f"{dead_jobs} job(s) dead after {MAX_ATTEMPTS} attempts")
    if email_relay_failures:
        who = ", ".join(
            f"{(f or {}).get('customer_name') or 'unknown'} (job {jid or '?'})"
            for jid, f, _ in stranded[:5]
        )
        needs_attention.append(
            f"{email_relay_failures} parsed lead(s) never reached GoHighLevel: {who}"
            + (" and others" if email_relay_failures > 5 else "")
        )
    if email_parse_failures_24h:
        needs_attention.append(
            f"{email_parse_failures_24h} work-order email(s) in the last 24h could not be read "
            f"— those leads were not relayed"
        )
    if stuck_recordings:
        problems.append(f"{stuck_recordings} recent recording(s) downloaded >6h ago but never "
                        f"transcribed — the pipeline may be stalled between stages")
    if call_age is not None and call_age > STALE_CALLS_MINUTES:
        problems.append(f"no call ingested in {round(call_age / 60)}h")
    if mail_age is not None and mail_age > STALE_MAIL_MINUTES and emails_24h == 0:
        problems.append(f"no email ingested in {round(mail_age / 60)}h")
    if settings.ASTERISK_ENABLED and telephony["ari_reachable"] is False:
        problems.append("Asterisk ARI is unreachable")

    # Only `problems` decide health. A lead GoHighLevel rejected is not the platform
    # malfunctioning — but it is still surfaced in the summary, unconditionally, because a
    # caller that reads only `status` must not miss it.
    healthy = not problems
    parts = ["Pipeline healthy." if healthy else "Pipeline DEGRADED: " + "; ".join(problems) + "."]
    if needs_attention:
        parts.append("Needs attention (not a malfunction): " + "; ".join(needs_attention) + ".")

    return ok(
        summary=" ".join(parts),
        data={
            "status": "healthy" if healthy else "degraded",
            "problems": problems,
            "needs_attention": needs_attention,
            "stranded_leads": [
                {
                    "job_id": jid,
                    "customer": (f or {}).get("customer_name"),
                    "service": (f or {}).get("service"),
                    "received_at": ts.isoformat() if ts else None,
                }
                for jid, f, ts in stranded
            ],
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
                # Counted apart from `dead` on purpose: the provider deleted the media, so
                # these can never succeed and are not an OWEN fault.
                "dead_media_gone_upstream": dead_gone_upstream,
                "oldest_pending_run_after": oldest_pending.isoformat() if oldest_pending else None,
            },
            "relays": {
                "email_parse_failures_24h": email_parse_failures_24h,
                "email_ignored_non_job_24h": email_ignored_24h,
                "email_relay_failures_total": email_relay_failures,
                # Off by configuration, not broken: null when GHL_CALL_WEBHOOK_URL is unset,
                # in which case no call is ever meant to reach GoHighLevel.
                "call_relay_to_ghl_enabled": call_relay_enabled,
                "calls_24h_not_relayed_to_ghl": unrelayed_calls,
            },
            "recordings": {
                "recent_downloaded_but_never_transcribed": stuck_recordings,
                # Informational, never a problem: the historical backfill copied audio
                # deliberately without transcribing it.
                "archive_untranscribed_by_design": untranscribed_archive,
            },
            "telephony": telephony,
        },
        notes=[
            "`status` reflects the PLATFORM only. `needs_attention` is separate: those items "
            "mean the software worked and a business outcome still needs a person. Report both.",
            "`email_ignored_non_job_24h` counts Dispatch mail that is not a work order "
            "(cancellations, notes, account mail). It is normal traffic, not a failure, and "
            "carries no lead.",
            "`calls_24h_not_relayed_to_ghl` is null when `call_relay_to_ghl_enabled` is false: "
            "the completed-call relay to GoHighLevel is switched off (GHL_CALL_WEBHOOK_URL is "
            "unset), so no call is expected to reach the CRM and there is no backlog. Email "
            "relaying is separate and unaffected.",
            "`dead_media_gone_upstream` and `archive_untranscribed_by_design` are reported "
            "for completeness and are NOT problems: the first is media the telephony provider "
            "deleted on its own retention schedule, the second is the historical backfill, "
            "which copied audio without transcribing it on purpose. Do not report either as "
            "something to fix.",
            "Call ingestion is polled every 5 minutes; a quiet night is normal for this business, "
            "so call staleness is only reported after 24h of silence.",
        ],
    )

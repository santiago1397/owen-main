"""`/api/ai/errors` — "what is going wrong", from the three places OWEN records failure.

A single log stream would miss most of it. OWEN's failures land in three different shapes:

1. **Captured log records** (`app_logs`) — exceptions and warnings from the app and worker.
2. **Dead and failing jobs** (`jobs.last_error`) — the recording/transcribe/analyze/relay
   pipeline retries five times and then gives up quietly. That give-up is invisible in logs
   after rotation but permanent in the table.
3. **Failed email parses and relays** (`inbound_emails`) — a Dispatch template change means
   leads are being stored but never reaching GoHighLevel. Nothing throws; the row is just
   flagged.

They are unioned into one time-ordered list with a common shape, so a caller asks one question
instead of three and cannot forget the third.

Requires the `logs` scope: error text routinely embeds phone numbers and provider identifiers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ai.deps import AuthedKey, require_scope
from app.api.ai.envelope import ok
from app.core.apikeys import SCOPE_LOGS
from app.core.config import settings
from app.core.logcapture import dropped_count
from app.db import get_db
from app.models import AppLog, InboundEmail, Job
from app.services.queue import MAX_ATTEMPTS

router = APIRouter(prefix="/api/ai", tags=["ai"])

_SINCE_UNITS = {"m": 1, "h": 60, "d": 1440}


def _parse_since(since: str) -> timedelta:
    """Accept '30m' / '6h' / '7d'. Invalid input falls back to 24h rather than erroring —
    a malformed lookback should still show you your errors."""
    try:
        unit = since[-1].lower()
        return timedelta(minutes=int(since[:-1]) * _SINCE_UNITS[unit])
    except (ValueError, KeyError, IndexError):
        return timedelta(hours=24)


@router.get("/errors")
async def errors(
    since: str = Query("24h", description="lookback window: 30m | 6h | 7d"),
    source: str | None = Query(None, description="logs | jobs | emails — default: all three"),
    level: str | None = Query(None, description="filter captured logs: WARNING | ERROR | CRITICAL"),
    service: str | None = Query(None, description="app | worker"),
    linkedid: str | None = Query(None, description="only records correlated to this call"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    _: AuthedKey = Depends(require_scope(SCOPE_LOGS)),
) -> dict:
    cutoff = datetime.now(timezone.utc) - _parse_since(since)
    want = {s.strip() for s in (source.split(",") if source else ["logs", "jobs", "emails"])}
    items: list[dict] = []

    if "logs" in want:
        where = [AppLog.at >= cutoff]
        if level:
            where.append(AppLog.level == level.strip().upper())
        if service:
            where.append(AppLog.service == service.strip().lower())
        if linkedid:
            where.append(AppLog.linkedid == linkedid.strip())
        rows = (await db.execute(
            select(AppLog).where(*where).order_by(AppLog.at.desc()).limit(limit)
        )).scalars().all()
        items += [
            {
                "at": r.at.isoformat() if r.at else None,
                "source": "log", "severity": r.level, "service": r.service,
                "origin": r.logger, "message": r.message,
                "linkedid": r.linkedid, "detail": r.traceback,
            }
            for r in rows
        ]

    if "jobs" in want and not linkedid:
        rows = (await db.execute(
            select(Job).where(
                Job.last_error.is_not(None),
                Job.created_at >= cutoff,
                Job.status.in_(("failed", "pending")),
            ).order_by(Job.created_at.desc()).limit(limit)
        )).scalars().all()
        items += [
            {
                "at": r.created_at.isoformat() if r.created_at else None,
                "source": "job",
                # A job still retrying is a warning; one that exhausted its attempts is an
                # error, because nothing will pick it up again without a human.
                "severity": "ERROR" if r.status == "failed" else "WARNING",
                "service": "worker", "origin": f"job:{r.type}",
                "message": (r.last_error or "")[:2000],
                "detail": f"job_id={r.id} status={r.status} attempts={r.attempts}/{MAX_ATTEMPTS}",
                "dead": r.status == "failed" and r.attempts >= MAX_ATTEMPTS,
            }
            for r in rows
        ]

    if "emails" in want and not linkedid:
        # `parse_status='ignored'` is deliberately absent: those are Dispatch emails that were
        # never work orders (cancellations, notes, account mail). They carry no lead and
        # nothing went wrong, so they are not errors and do not belong in this list at all.
        rows = (await db.execute(
            select(InboundEmail).where(
                InboundEmail.received_at >= cutoff,
                (InboundEmail.parse_status == "failed") | (InboundEmail.relay_status == "failed"),
            ).order_by(InboundEmail.received_at.desc()).limit(limit)
        )).scalars().all()
        for r in rows:
            # A lead GoHighLevel rejected is not a software error — the pipeline did its job
            # and the CRM said no. Classifying it as ERROR both overstates the fault and
            # buries a real, actionable business item among stack traces. It gets its own
            # source and severity so a caller can act on it without triaging it first.
            stranded = r.relay_status == "failed"
            customer = (r.fields or {}).get("customer_name") if r.fields else None
            items.append({
                "at": r.received_at.isoformat() if r.received_at else None,
                "source": "lost_lead" if stranded else "email",
                "severity": "ACTION_REQUIRED" if stranded else "ERROR",
                "service": "worker",
                "origin": f"email:{r.source or 'unknown'}",
                "message": (
                    f"Lead not delivered to GoHighLevel: {customer or 'unknown customer'} "
                    f"(job {r.job_id or '?'}). {(r.relay_error or '').splitlines()[0][:300]}"
                    if stranded else (r.parse_error or "work-order email could not be read")[:2000]
                ),
                "detail": f"email_id={r.id} job_id={r.job_id} parse={r.parse_status} "
                          f"relay={r.relay_status} subject={r.subject!r}",
                "customer": customer,
                "job_id": r.job_id,
                # Both are recoverable by re-running the relay once the cause is fixed.
                "retry": f"POST /api/emails/{r.id}/relay" if stranded else None,
            })

    items.sort(key=lambda i: i["at"] or "", reverse=True)
    items = items[:limit]

    counts: dict[str, int] = {}
    for item in items:
        counts[item["source"]] = counts.get(item["source"], 0) + 1

    total_captured = (await db.execute(
        select(func.count()).select_from(AppLog).where(AppLog.at >= cutoff)
    )).scalar_one()

    notes = [
        "source='lost_lead' (severity ACTION_REQUIRED) is NOT a software error: the email "
        "parsed correctly and GoHighLevel rejected it. Each one is a real customer who never "
        "reached the CRM — report them as business items to chase, not as bugs. Retry with "
        "the `retry` field once the cause is fixed.",
        "Dispatch mail that is not a work order (cancellations, notes, account mail) is "
        "classified 'ignored' and never appears here — it carries no lead and nothing failed.",
        f"Captured logs start at {settings.APP_LOG_CAPTURE_LEVEL} and are retained "
        f"{settings.APP_LOG_RETENTION_DAYS} days. Anything below that level exists only in the "
        f"container's Docker logs on the VPS.",
        "Log capture is not retroactive: nothing from before this feature was deployed appears here.",
    ]
    dropped = dropped_count()
    if dropped:
        notes.append(f"{dropped} log record(s) were dropped by this process under load and are "
                     f"NOT in the database.")

    # Lost leads are counted and described separately so a caller never reports them as
    # "N errors" — they are the one category here that costs money rather than uptime.
    lost = counts.get("lost_lead", 0)
    faults = len(items) - lost
    if not items:
        summary = f"No errors in the last {since}."
    else:
        bits = []
        if faults:
            bits.append(f"{faults} error/warning record(s)")
        if lost:
            bits.append(f"{lost} undelivered lead(s) needing action (not a malfunction)")
        summary = f"In the last {since}: " + " and ".join(bits) + "."
    return ok(
        summary=summary,
        data={"items": items, "counts_by_source": counts, "captured_logs_in_window": total_captured},
        applied_filters={"since": since, "cutoff": cutoff.isoformat(), "source": sorted(want),
                         "level": level, "service": service, "linkedid": linkedid, "limit": limit},
        notes=notes,
    )

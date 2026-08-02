"""Endpoints that return what people actually said — gated on the `content` scope.

Counts tell you a campaign is producing calls; only content tells you those calls are all
warranty questions. That is the difference between a metrics API and a useful one, so it is
here — behind its own scope, because these responses carry customer names, addresses, phone
numbers and full transcripts, and a key handed to a third-party integration should not get
them by default.

`/calls/recent` is one call per row with its AI summary; `/calls/{id}/transcript` is the full
text of one call. Both are deliberately narrow: an AI that wants to bulk-mine transcripts should
use /query, where the request is audited with its SQL.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ai import periods
from app.api.ai.deps import AuthedKey, require_scope, resolve_window
from app.api.ai.envelope import NOTE_JUNK, NOTE_JUNK_INCLUDED, error_detail, ok
from app.api.ai.filters import call_filters
from app.core.apikeys import SCOPE_CONTENT
from app.db import get_db
from app.models import (
    Call,
    CallAnalysis,
    Caller,
    Campaign,
    InboundEmail,
    Number,
    Transcription,
)

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.get("/calls/recent")
async def recent_calls(
    period: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    min_duration: int | None = Query(None, ge=0),
    max_duration: int | None = Query(None, ge=0),
    include_junk: bool = False,
    with_summary: bool = Query(True, description="include the AI summary and category"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: AuthedKey = Depends(require_scope(SCOPE_CONTENT)),
) -> dict:
    """Recent calls with attribution and, optionally, what the AI made of each one."""
    start, end, described = resolve_window(period, date_from, date_to)
    where = call_filters(start, end, include_junk, min_duration, max_duration)

    total = (await db.execute(select(func.count()).select_from(Call).where(*where))).scalar_one()
    rows = (await db.execute(
        select(
            Call.id, Call.started_at, Call.duration_seconds, Call.status, Call.direction,
            Call.answered_at, Call.is_new_for_campaign,
            Caller.phone_number, Campaign.name, Number.phone_number, Number.friendly_name,
            func.coalesce(CallAnalysis.category_override, CallAnalysis.category),
            CallAnalysis.summary, CallAnalysis.tags,
        )
        .select_from(Call)
        .join(Caller, Call.caller_id == Caller.id, isouter=True)
        .join(Campaign, Call.campaign_id == Campaign.id, isouter=True)
        .join(Number, Call.number_id == Number.id, isouter=True)
        .join(CallAnalysis, CallAnalysis.call_id == Call.id, isouter=True)
        .where(*where)
        .order_by(Call.started_at.desc())
        .limit(limit).offset(offset)
    )).all()

    items = []
    for (cid, started, dur, st, direction, answered, is_new, caller, camp,
         num, friendly, category, summary, tags) in rows:
        item = {
            "call_id": str(cid),
            "started_at": started.isoformat() if started else None,
            "duration_seconds": dur, "status": st, "direction": direction,
            "answered": answered is not None,
            "new_for_campaign": is_new,
            "caller": caller, "campaign": camp,
            "tracking_number": num, "tracking_number_name": friendly,
        }
        if with_summary:
            item.update({"category": category, "summary": summary, "tags": tags})
        items.append(item)

    return ok(
        summary=f"{len(items)} of {total} calls {periods.describe_window(described)}"
                f"{' with AI summaries' if with_summary else ''}.",
        data={"total_matching": total, "returned": len(items), "calls": items},
        applied_filters={**described, "include_junk": include_junk, "limit": limit, "offset": offset,
                         "min_duration_seconds": min_duration, "max_duration_seconds": max_duration},
        notes=[NOTE_JUNK_INCLUDED if include_junk else NOTE_JUNK,
               "`summary` and `category` are NULL for calls that were never recorded, "
               "transcribed and analyzed — most short calls."],
    )


@router.get("/calls/{call_id}/transcript")
async def call_transcript(
    call_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: AuthedKey = Depends(require_scope(SCOPE_CONTENT)),
) -> dict:
    """Full transcript, AI analysis and attribution for one call."""
    call = await db.get(Call, call_id)
    if call is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            error_detail("call_not_found", f"No call with id {call_id}.",
                         hint="Call ids come from /api/ai/calls/recent or a /api/ai/query result."),
        )
    tr = (await db.execute(
        select(Transcription).where(Transcription.call_id == call_id)
        .order_by(Transcription.id).limit(1)
    )).scalar_one_or_none()
    analysis = (await db.execute(
        select(CallAnalysis).where(CallAnalysis.call_id == call_id)
    )).scalar_one_or_none()

    if tr is None:
        summary_line = "This call has no transcript — it was never recorded, or the recording " \
                       "has not been transcribed yet."
    else:
        summary_line = f"Transcript of a {call.duration_seconds or '?'}s call " \
                       f"({len(tr.text or '')} characters)."

    return ok(
        summary=summary_line,
        data={
            "call_id": str(call_id),
            "started_at": call.started_at.isoformat() if call.started_at else None,
            "duration_seconds": call.duration_seconds,
            "status": call.status,
            "transcript": tr.text if tr else None,
            # Speaker-labeled turns exist only for dual-channel recordings.
            "segments": tr.segments if tr else None,
            "engine": tr.engine if tr else None,
            "analysis": {
                "category": (analysis.category_override or analysis.category) if analysis else None,
                "summary": analysis.summary if analysis else None,
                "tags": analysis.tags if analysis else None,
                "model": analysis.model if analysis else None,
            } if analysis else None,
        },
        notes=["Human category overrides win over the model's verdict and are what is returned here."],
    )


@router.get("/leads/recent")
async def recent_leads(
    period: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    source: str | None = None,
    parse_status: str = Query(
        "parsed", description="parsed | failed | ignored | all. 'ignored' is Dispatch mail "
                              "that was never a work order and contains no lead."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: AuthedKey = Depends(require_scope(SCOPE_CONTENT)),
) -> dict:
    """Individual leads from job-notification emails, with the extracted customer details.

    `parse_status=failed` is the human-inspect queue: work orders that could not be parsed,
    were never relayed to GoHighLevel, and represent lost leads. `ignored` is the opposite —
    mail that was never a work order (cancellations, notes, account mail); nothing was lost.
    """
    start, end, described = resolve_window(period, date_from, date_to)
    where = []
    if start is not None:
        where.append(InboundEmail.received_at >= start)
    where.append(InboundEmail.received_at < end)
    if source:
        where.append(InboundEmail.source == source.strip().lower())
    if parse_status != "all":
        where.append(InboundEmail.parse_status == parse_status)

    total = (await db.execute(
        select(func.count()).select_from(InboundEmail).where(*where)
    )).scalar_one()
    rows = (await db.execute(
        select(InboundEmail).where(*where)
        .order_by(InboundEmail.received_at.desc()).limit(limit).offset(offset)
    )).scalars().all()

    return ok(
        summary=f"{len(rows)} of {total} leads {periods.describe_window(described)} "
                f"(parse_status={parse_status}).",
        data={
            "total_matching": total,
            "leads": [
                {
                    "id": str(r.id), "received_at": r.received_at.isoformat() if r.received_at else None,
                    "source": r.source, "job_id": r.job_id, "subject": r.subject,
                    "parse_status": r.parse_status, "parse_error": r.parse_error,
                    "relayed_to_ghl": r.relayed_to_ghl, "relay_status": r.relay_status,
                    "fields": r.fields,
                }
                for r in rows
            ],
        },
        applied_filters={**described, "source": source, "parse_status": parse_status,
                         "limit": limit, "offset": offset},
        notes=["`fields` holds everything extracted from the email: customer name, phone, "
               "service address, brand, job type."],
    )

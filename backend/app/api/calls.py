import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db import get_db
from app.models import (
    Call,
    CallAnalysis,
    CallEvent,
    Caller,
    Campaign,
    Number,
    Provider,
    Recording,
    Transcription,
    User,
)
from app.schemas.api import (
    AnalysisOut,
    AnalysisOverride,
    CallDetail,
    CallEventOut,
    CallListItem,
    Page,
    RecordingOut,
)

router = APIRouter(prefix="/api/calls", tags=["calls"])


def _apply_filters(stmt, provider, number_id, campaign_id, caller, status_, date_from, date_to):
    if provider:
        stmt = stmt.where(Provider.name == provider)
    if number_id:
        stmt = stmt.where(Call.number_id == number_id)
    if campaign_id:
        stmt = stmt.where(Call.campaign_id == campaign_id)
    if caller:
        stmt = stmt.where(Caller.phone_number.ilike(f"%{caller}%"))
    if status_:
        stmt = stmt.where(Call.status == status_)
    if date_from:
        stmt = stmt.where(Call.started_at >= date_from)
    if date_to:
        stmt = stmt.where(Call.started_at <= date_to)
    return stmt


@router.get("", response_model=Page)
async def list_calls(
    provider: str | None = None,
    number_id: uuid.UUID | None = None,
    campaign_id: uuid.UUID | None = None,
    caller: str | None = None,
    status: str | None = None,  # noqa: A002 - query param name
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Page:
    base = (
        select(Call)
        .join(Provider, Call.provider_id == Provider.id, isouter=True)
        .join(Caller, Call.caller_id == Caller.id, isouter=True)
    )
    base = _apply_filters(base, provider, number_id, campaign_id, caller, status, date_from, date_to)

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()

    rows_stmt = (
        select(
            Call.id,
            Call.provider_call_sid,
            Call.direction,
            Call.status,
            Call.started_at,
            Call.duration_seconds,
            Call.is_new_for_campaign,
            Provider.name.label("provider"),
            Caller.phone_number.label("caller_number"),
            Number.phone_number.label("dialed_number"),
            Campaign.name.label("campaign_name"),
            exists().where(Recording.call_id == Call.id).label("has_recording"),
            func.coalesce(CallAnalysis.category_override, CallAnalysis.category).label("category"),
            func.coalesce(CallAnalysis.is_spam_override, CallAnalysis.is_spam).label("is_spam"),
        )
        .join(Provider, Call.provider_id == Provider.id, isouter=True)
        .join(Caller, Call.caller_id == Caller.id, isouter=True)
        .join(Number, Call.number_id == Number.id, isouter=True)
        .join(Campaign, Call.campaign_id == Campaign.id, isouter=True)
        .join(CallAnalysis, CallAnalysis.call_id == Call.id, isouter=True)
    )
    rows_stmt = _apply_filters(rows_stmt, provider, number_id, campaign_id, caller, status, date_from, date_to)
    rows_stmt = rows_stmt.order_by(Call.started_at.desc().nullslast()).offset((page - 1) * page_size).limit(page_size)

    rows = (await db.execute(rows_stmt)).mappings().all()
    items = [CallListItem(**dict(r)) for r in rows]
    return Page(items=items, page=page, page_size=page_size, total=total)


@router.get("/{call_id}", response_model=CallDetail)
async def get_call(
    call_id: uuid.UUID,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> CallDetail:
    row = (
        await db.execute(
            select(
                Call.id, Call.provider_call_sid, Call.direction, Call.status, Call.started_at,
                Call.answered_at, Call.ended_at, Call.duration_seconds, Call.forwarded_to,
                Call.is_new_for_campaign,
                Provider.name.label("provider"),
                Caller.phone_number.label("caller_number"),
                Number.phone_number.label("dialed_number"),
                Campaign.name.label("campaign_name"),
                exists().where(Recording.call_id == Call.id).label("has_recording"),
            )
            .join(Provider, Call.provider_id == Provider.id, isouter=True)
            .join(Caller, Call.caller_id == Caller.id, isouter=True)
            .join(Number, Call.number_id == Number.id, isouter=True)
            .join(Campaign, Call.campaign_id == Campaign.id, isouter=True)
            .where(Call.id == call_id)
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "call not found")

    events = (
        await db.execute(
            select(CallEvent.event_type, CallEvent.received_at)
            .where(CallEvent.call_id == call_id)
            .order_by(CallEvent.received_at)
        )
    ).mappings().all()
    recs = (
        await db.execute(
            select(Recording.id, Recording.status, Recording.duration_seconds, Recording.storage_path)
            .where(Recording.call_id == call_id)
        )
    ).mappings().all()

    transcript = (
        await db.execute(
            select(Transcription.text).where(Transcription.call_id == call_id)
            .order_by(Transcription.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()

    analysis_row = (
        await db.execute(select(CallAnalysis).where(CallAnalysis.call_id == call_id))
    ).scalar_one_or_none()

    data = dict(row)
    # effective category/is_spam (override wins) for the detail header too
    if analysis_row:
        data["category"] = analysis_row.category_override or analysis_row.category
        data["is_spam"] = (analysis_row.is_spam_override
                           if analysis_row.is_spam_override is not None else analysis_row.is_spam)

    return CallDetail(
        **data,
        events=[CallEventOut(**dict(e)) for e in events],
        recordings=[
            RecordingOut(id=r["id"], status=r["status"], duration_seconds=r["duration_seconds"],
                         available=bool(r["storage_path"]))
            for r in recs
        ],
        transcript=transcript,
        analysis=AnalysisOut(
            is_spam=analysis_row.is_spam, spam_confidence=float(analysis_row.spam_confidence)
            if analysis_row.spam_confidence is not None else None,
            category=analysis_row.category, tags=analysis_row.tags or [],
            summary=analysis_row.summary, model=analysis_row.model,
            category_override=analysis_row.category_override,
            is_spam_override=analysis_row.is_spam_override,
        ) if analysis_row else None,
    )


@router.patch("/{call_id}/analysis", response_model=AnalysisOut)
async def override_analysis(
    call_id: uuid.UUID,
    body: AnalysisOverride,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AnalysisOut:
    """Human correction of the LLM's category/spam verdict (decision #5, #11)."""
    row = (await db.execute(select(CallAnalysis).where(CallAnalysis.call_id == call_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no analysis for this call yet")
    if body.category_override is not None:
        row.category_override = body.category_override
    if body.is_spam_override is not None:
        row.is_spam_override = body.is_spam_override
    await db.commit()
    return AnalysisOut(
        is_spam=row.is_spam, spam_confidence=float(row.spam_confidence) if row.spam_confidence is not None else None,
        category=row.category, tags=row.tags or [], summary=row.summary, model=row.model,
        category_override=row.category_override, is_spam_override=row.is_spam_override,
    )

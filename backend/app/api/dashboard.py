from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.api.junk import IS_JUNK, NOT_JUNK
from app.core.config import settings
from app.db import get_db
from app.models import Call, CallAnalysis, Caller, Campaign, Number, User
from app.schemas.api import DashboardSummary

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/summary", response_model=DashboardSummary)
async def summary(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    hide_junk: bool = True,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> DashboardSummary:
    now = datetime.now(timezone.utc)
    # Frontend sends explicit UTC bounds (half-open: date_to is start-of-next-day so the
    # last day is fully included). Absent both, default to the last 7 days.
    start = date_from or (now - timedelta(days=7))
    end = date_to or now
    in_range = and_(Call.started_at >= start, Call.started_at < end)

    # Every call-based aggregate shares this filter. By default likely-junk calls
    # (short / never-connected) are excluded from the stats; flip hide_junk=false to keep them.
    call_filters = [in_range]
    if hide_junk:
        call_filters.append(NOT_JUNK)

    total_calls = (await db.execute(select(func.count()).select_from(Call).where(*call_filters))).scalar_one()
    spam_calls = (await db.execute(
        select(func.count()).select_from(Call)
        .join(CallAnalysis, CallAnalysis.call_id == Call.id)
        .where(*call_filters, func.coalesce(CallAnalysis.is_spam_override, CallAnalysis.is_spam).is_(True))
    )).scalar_one()
    avg_duration = (
        await db.execute(select(func.avg(Call.duration_seconds)).where(*call_filters, Call.duration_seconds.is_not(None)))
    ).scalar_one()

    # Likely-junk count is informational: always measured over the full range, independent
    # of the hide_junk toggle (that's the whole point of the card — show what's being hidden).
    junk_calls = (await db.execute(
        select(func.count()).select_from(Call).where(in_range, IS_JUNK)
    )).scalar_one()

    # Per-campaign new vs returning (call-level flag stamped at ingest).
    new_for_campaign = (await db.execute(
        select(func.count()).select_from(Call).where(*call_filters, Call.is_new_for_campaign.is_(True))
    )).scalar_one()
    returning_for_campaign = (await db.execute(
        select(func.count()).select_from(Call).where(*call_filters, Call.is_new_for_campaign.is_(False))
    )).scalar_one()

    # Global new vs returning: distinct callers active (non-junk) in range, split by whether
    # their first-ever sighting falls inside the window.
    distinct_callers = (await db.execute(
        select(func.count(func.distinct(Call.caller_id))).where(*call_filters, Call.caller_id.is_not(None))
    )).scalar_one()
    new_global = (await db.execute(
        select(func.count(func.distinct(Call.caller_id)))
        .join(Caller, Call.caller_id == Caller.id)
        .where(*call_filters, Call.caller_id.is_not(None), Caller.first_seen_at >= start)
    )).scalar_one()
    returning_global = max(distinct_callers - new_global, 0)

    by_campaign = [
        {"campaign": name or "(unattributed)", "calls": count}
        for name, count in (await db.execute(
            select(Campaign.name, func.count(Call.id))
            .join(Campaign, Call.campaign_id == Campaign.id, isouter=True)
            .where(*call_filters).group_by(Campaign.name).order_by(func.count(Call.id).desc())
        )).all()
    ]

    by_number = [
        {"number": num or "(unknown)", "friendly": friendly, "calls": count}
        for num, friendly, count in (await db.execute(
            select(Number.phone_number, Number.friendly_name, func.count(Call.id))
            .join(Number, Call.number_id == Number.id, isouter=True)
            .where(*call_filters).group_by(Number.phone_number, Number.friendly_name)
            .order_by(func.count(Call.id).desc()).limit(20)
        )).all()
    ]

    # Daily series bucketed in the business timezone (decision #10).
    local_ts = func.timezone(settings.BUSINESS_TZ, Call.started_at)
    day = func.date_trunc("day", local_ts)
    daily = [
        {"day": d.date().isoformat() if d else None, "calls": count}
        for d, count in (await db.execute(
            select(day.label("day"), func.count(Call.id)).where(*call_filters).group_by("day").order_by("day")
        )).all()
    ]

    # Hour-of-day histogram in business tz (Miami/Eastern). Zero-fill all 24 hours so the
    # frontend renders a gapless 0–23 axis regardless of which hours saw traffic.
    hour_expr = func.extract("hour", local_ts)
    hour_counts = {
        int(h): count
        for h, count in (await db.execute(
            select(hour_expr.label("hour"), func.count(Call.id)).where(*call_filters).group_by("hour")
        )).all()
    }
    by_hour = [{"hour": h, "calls": hour_counts.get(h, 0)} for h in range(24)]

    top_callers = [
        {"phone": phone, "calls": count}
        for phone, count in (await db.execute(
            select(Caller.phone_number, func.count(Call.id))
            .join(Caller, Call.caller_id == Caller.id)
            .where(*call_filters).group_by(Caller.phone_number)
            .order_by(func.count(Call.id).desc()).limit(10)
        )).all()
    ]

    return DashboardSummary(
        range_from=start, range_to=end, total_calls=total_calls, spam_calls=spam_calls,
        junk_calls=junk_calls,
        avg_duration_seconds=float(avg_duration) if avg_duration is not None else None,
        new_callers_global=new_global, returning_callers_global=returning_global,
        new_for_campaign=new_for_campaign, returning_for_campaign=returning_for_campaign,
        by_campaign=by_campaign, by_number=by_number, daily=daily, by_hour=by_hour,
        top_callers=top_callers,
    )

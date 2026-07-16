from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SHORT_CALL_MAX_DURATION_SECONDS, current_user
from app.core.config import settings
from app.db import get_db
from app.models import Call, CallAnalysis, Caller, Campaign, Number, User
from app.schemas.api import DashboardSummary

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

_RANGES = {"today": 1, "7d": 7, "30d": 30, "90d": 90}


@router.get("/summary", response_model=DashboardSummary)
async def summary(
    range: str = "7d",  # noqa: A002
    include_short: bool = False,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> DashboardSummary:
    days = _RANGES.get(range, 7)
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    in_range = Call.started_at >= start

    # Every call-based aggregate shares this filter, so 0–1s misdial/hang-up junk is
    # excluded from the stats by default (matches the calls list). Pass include_short=true
    # to count them. NULL durations (never reported) are kept.
    call_filters = [in_range]
    if not include_short:
        call_filters.append(
            or_(
                Call.duration_seconds.is_(None),
                Call.duration_seconds > SHORT_CALL_MAX_DURATION_SECONDS,
            )
        )

    total_calls = (await db.execute(select(func.count()).select_from(Call).where(*call_filters))).scalar_one()
    spam_calls = (await db.execute(
        select(func.count()).select_from(Call)
        .join(CallAnalysis, CallAnalysis.call_id == Call.id)
        .where(*call_filters, func.coalesce(CallAnalysis.is_spam_override, CallAnalysis.is_spam).is_(True))
    )).scalar_one()
    avg_duration = (
        await db.execute(select(func.avg(Call.duration_seconds)).where(*call_filters, Call.duration_seconds.is_not(None)))
    ).scalar_one()

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
        range_from=start, range_to=now, total_calls=total_calls, spam_calls=spam_calls,
        avg_duration_seconds=float(avg_duration) if avg_duration is not None else None,
        new_callers_global=new_global, returning_callers_global=returning_global,
        new_for_campaign=new_for_campaign, returning_for_campaign=returning_for_campaign,
        by_campaign=by_campaign, by_number=by_number, daily=daily, top_callers=top_callers,
    )

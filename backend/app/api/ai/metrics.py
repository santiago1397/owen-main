"""Curated metric endpoints — the questions worth answering without writing SQL.

Every endpoint here takes the same time arguments (`period` or `date_from`/`date_to`), returns
the same envelope, and states in `applied_filters` exactly what it counted. Anything not
covered lives in `/api/ai/query`.

Percentiles come from `percentile_cont`, so "median call length" is the real median rather than
a mean pretending to be one — the mean is dragged badly by the long tail of 15-second calls.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Float, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ai import periods
from app.api.ai.deps import AuthedKey, require_scope
from app.api.ai.envelope import (
    NOTE_JUNK,
    NOTE_JUNK_INCLUDED,
    NOTE_PHANTOM,
    NOTE_SPAM_DEAD,
    error_detail,
    ok,
)
from app.api.ai.filters import IS_JUNK, REAL_CALL, call_filters
from app.core.apikeys import SCOPE_READ
from app.core.config import settings
from app.db import get_db
from app.models import (
    Call,
    CallAnalysis,
    CallCharge,
    Caller,
    Campaign,
    InboundEmail,
    Message,
    Number,
)

router = APIRouter(prefix="/api/ai", tags=["ai"])


def _window(period, date_from, date_to):
    try:
        return periods.resolve(period, date_from, date_to)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            error_detail(
                "unknown_period",
                f"Unknown period {str(exc)!r}.",
                hint="Use one of the named periods, or pass explicit date_from/date_to.",
                valid_periods=periods.PERIODS,
            ),
        ) from exc


def _series(rows) -> list[dict]:
    return [{"day": d.date().isoformat() if d else None, "count": c} for d, c in rows]


# --- calls ---------------------------------------------------------------------------
@router.get("/calls/stats")
async def call_stats(
    period: str | None = Query(None, description="today | yesterday | last_7d | this_week | last_month | ..."),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    min_duration: int | None = Query(None, ge=0, description="seconds, inclusive"),
    max_duration: int | None = Query(None, ge=0, description="seconds, inclusive — 'under 45s' is max_duration=45"),
    campaign: str | None = Query(None, description="campaign name (exact, case-insensitive)"),
    number: str | None = Query(None, description="tracking number in E.164, e.g. +13055559999"),
    direction: str | None = Query(None, description="inbound | outbound"),
    call_status: str | None = Query(None, alias="status"),
    answered: bool | None = Query(None, description="true = the provider reported an answer time"),
    new_callers: bool | None = Query(None, description="true = first-ever call to that campaign"),
    include_junk: bool = Query(False, description="include <=13s and never-connected calls"),
    group_by: str = Query("day", description="day | hour_of_day | campaign | number | status | none"),
    db: AsyncSession = Depends(get_db),
    _: AuthedKey = Depends(require_scope(SCOPE_READ)),
) -> dict:
    """Call volume and duration for any window, with any combination of filters.

    This is the workhorse: "how many calls did we get last week", "how many were under 45
    seconds", "how many from the AHS campaign", "how many went unanswered".
    """
    start, end, described = _window(period, date_from, date_to)

    campaign_id = number_id = None
    if campaign:
        campaign_id = (await db.execute(
            select(Campaign.id).where(func.lower(Campaign.name) == campaign.strip().lower())
        )).scalar_one_or_none()
        if campaign_id is None:
            names = (await db.execute(select(Campaign.name).order_by(Campaign.name))).scalars().all()
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                error_detail("unknown_campaign", f"No campaign named {campaign!r}.",
                             hint="Use one of the known campaign names.", known_campaigns=names),
            )
    if number:
        number_id = (await db.execute(
            select(Number.id).where(Number.phone_number == number.strip())
        )).scalar_one_or_none()
        if number_id is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                error_detail("unknown_number", f"No tracking number {number!r}.",
                             hint="Numbers are stored in E.164, e.g. +13055559999. "
                                  "List them via /api/ai/query or the Numbers page."),
            )

    where = call_filters(
        start, end, include_junk, min_duration, max_duration, campaign_id, number_id,
        direction, call_status, answered, new_callers,
    )

    total = (await db.execute(select(func.count()).select_from(Call).where(*where))).scalar_one()

    dur = Call.duration_seconds
    has_dur = [*where, dur.is_not(None)]
    stats_row = (await db.execute(
        select(
            func.avg(cast(dur, Float)),
            func.percentile_cont(0.5).within_group(cast(dur, Float)),
            func.percentile_cont(0.9).within_group(cast(dur, Float)),
            func.min(dur), func.max(dur), func.sum(dur),
        ).select_from(Call).where(*has_dur)
    )).first()
    avg_s, p50, p90, min_s, max_s, total_s = stats_row or (None,) * 6

    answered_count = (await db.execute(
        select(func.count()).select_from(Call).where(*where, Call.answered_at.is_not(None))
    )).scalar_one()
    new_count = (await db.execute(
        select(func.count()).select_from(Call).where(*where, Call.is_new_for_campaign.is_(True))
    )).scalar_one()
    unique_callers = (await db.execute(
        select(func.count(func.distinct(Call.caller_id))).where(*where, Call.caller_id.is_not(None))
    )).scalar_one()
    # Always reported over the same window regardless of include_junk — this is the number
    # that explains a gap between OWEN's dashboard and a raw provider report.
    junk_count = (await db.execute(
        select(func.count()).select_from(Call).where(
            REAL_CALL,
            *( [Call.started_at >= start] if start is not None else [] ),
            Call.started_at < end, IS_JUNK,
        )
    )).scalar_one()

    local_ts = func.timezone(settings.BUSINESS_TZ, Call.started_at)
    breakdown: list[dict] = []
    if group_by == "day":
        day = func.date_trunc("day", local_ts)
        breakdown = _series((await db.execute(
            select(day.label("d"), func.count(Call.id)).where(*where).group_by("d").order_by("d")
        )).all())
    elif group_by == "hour_of_day":
        hour = func.extract("hour", local_ts)
        counts = {int(h): c for h, c in (await db.execute(
            select(hour.label("h"), func.count(Call.id)).where(*where).group_by("h")
        )).all()}
        breakdown = [{"hour": h, "count": counts.get(h, 0)} for h in range(24)]
    elif group_by == "campaign":
        breakdown = [
            {"campaign": name or "(unattributed)", "count": c}
            for name, c in (await db.execute(
                select(Campaign.name, func.count(Call.id))
                .select_from(Call).join(Campaign, Call.campaign_id == Campaign.id, isouter=True)
                .where(*where).group_by(Campaign.name).order_by(func.count(Call.id).desc())
            )).all()
        ]
    elif group_by == "number":
        breakdown = [
            {"number": n or "(unknown)", "friendly": f, "count": c}
            for n, f, c in (await db.execute(
                select(Number.phone_number, Number.friendly_name, func.count(Call.id))
                .select_from(Call).join(Number, Call.number_id == Number.id, isouter=True)
                .where(*where).group_by(Number.phone_number, Number.friendly_name)
                .order_by(func.count(Call.id).desc()).limit(50)
            )).all()
        ]
    elif group_by == "status":
        breakdown = [
            {"status": s or "(unknown)", "count": c}
            for s, c in (await db.execute(
                select(Call.status, func.count(Call.id)).where(*where)
                .group_by(Call.status).order_by(func.count(Call.id).desc())
            )).all()
        ]
    elif group_by != "none":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            error_detail("unknown_group_by", f"Unknown group_by {group_by!r}.",
                         hint="Valid: day, hour_of_day, campaign, number, status, none."),
        )

    window = periods.describe_window(described)
    bits = [f"{total} calls {window}"]
    if max_duration is not None:
        bits.append(f"filtered to calls of {max_duration}s or less")
    if min_duration is not None:
        bits.append(f"filtered to calls of {min_duration}s or more")
    if campaign:
        bits.append(f"campaign {campaign!r}")
    if p50 is not None:
        bits.append(f"median {round(float(p50))}s, average {round(float(avg_s or 0))}s")
    bits.append(f"{answered_count} answered")

    notes = [NOTE_PHANTOM, NOTE_JUNK_INCLUDED if include_junk else NOTE_JUNK]

    return ok(
        summary="; ".join(bits) + ".",
        data={
            "total_calls": total,
            "answered_calls": answered_count,
            "unanswered_calls": total - answered_count,
            "unique_callers": unique_callers,
            "new_for_campaign": new_count,
            "returning_for_campaign": total - new_count,
            "junk_calls_in_window": junk_count,
            "duration_seconds": {
                "average": round(float(avg_s), 1) if avg_s is not None else None,
                "median": round(float(p50), 1) if p50 is not None else None,
                "p90": round(float(p90), 1) if p90 is not None else None,
                "min": min_s, "max": max_s, "total": int(total_s) if total_s is not None else 0,
            },
            "group_by": group_by,
            "breakdown": breakdown,
        },
        applied_filters={
            **described,
            "include_junk": include_junk,
            "min_duration_seconds": min_duration,
            "max_duration_seconds": max_duration,
            "campaign": campaign, "number": number, "direction": direction,
            "status": call_status, "answered": answered, "new_callers": new_callers,
            "always_excluded": "rows with started_at IS NULL",
        },
        notes=notes,
    )


# --- leads (inbound job-notification emails) ------------------------------------------
@router.get("/leads/stats")
async def lead_stats(
    period: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    source: str | None = Query(None, description="e.g. 'dispatch' (American Home Shield jobs)"),
    group_by: str = Query("day", description="day | week | source | brand | none"),
    db: AsyncSession = Depends(get_db),
    _: AuthedKey = Depends(require_scope(SCOPE_READ)),
) -> dict:
    """New leads from inbound job-notification emails (Dispatch / American Home Shield).

    A "lead" here is a successfully PARSED email — those are the ones that become GHL contacts
    and opportunities. Emails that failed to parse are counted separately and never relayed;
    a rising `parse_failed` count means the sender changed their template and leads are being
    dropped on the floor, so it is surfaced in the summary rather than hidden.
    """
    start, end, described = _window(period, date_from, date_to)

    base = []
    if start is not None:
        base.append(InboundEmail.received_at >= start)
    base.append(InboundEmail.received_at < end)
    if source:
        base.append(InboundEmail.source == source.strip().lower())

    parsed = [*base, InboundEmail.parse_status == "parsed"]
    total = (await db.execute(select(func.count()).select_from(InboundEmail).where(*parsed))).scalar_one()
    failed = (await db.execute(
        select(func.count()).select_from(InboundEmail).where(*base, InboundEmail.parse_status == "failed")
    )).scalar_one()
    relayed = (await db.execute(
        select(func.count()).select_from(InboundEmail).where(*parsed, InboundEmail.relayed_to_ghl.is_(True))
    )).scalar_one()
    relay_failed = (await db.execute(
        select(func.count()).select_from(InboundEmail).where(*base, InboundEmail.relay_status == "failed")
    )).scalar_one()
    unique_jobs = (await db.execute(
        select(func.count(func.distinct(InboundEmail.job_id))).where(*parsed, InboundEmail.job_id.is_not(None))
    )).scalar_one()

    local_ts = func.timezone(settings.BUSINESS_TZ, InboundEmail.received_at)
    breakdown: list[dict] = []
    if group_by in ("day", "week"):
        bucket = func.date_trunc(group_by, local_ts)
        breakdown = [
            {group_by: d.date().isoformat() if d else None, "count": c}
            for d, c in (await db.execute(
                select(bucket.label("b"), func.count(InboundEmail.id))
                .where(*parsed).group_by("b").order_by("b")
            )).all()
        ]
    elif group_by == "source":
        breakdown = [
            {"source": s or "(unknown)", "count": c}
            for s, c in (await db.execute(
                select(InboundEmail.source, func.count(InboundEmail.id))
                .where(*parsed).group_by(InboundEmail.source).order_by(func.count(InboundEmail.id).desc())
            )).all()
        ]
    elif group_by == "brand":
        # `brand` lives inside the parsed JSON (e.g. "American Home Shield"), not a column.
        brand = InboundEmail.fields["brand"].astext
        breakdown = [
            {"brand": b or "(unknown)", "count": c}
            for b, c in (await db.execute(
                select(brand.label("b"), func.count(InboundEmail.id))
                .where(*parsed).group_by("b").order_by(func.count(InboundEmail.id).desc())
            )).all()
        ]
    elif group_by != "none":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            error_detail("unknown_group_by", f"Unknown group_by {group_by!r}.",
                         hint="Valid: day, week, source, brand, none."),
        )

    window = periods.describe_window(described)
    summary = f"{total} new leads (parsed job emails) {window}; {relayed} reached GoHighLevel"
    if failed:
        summary += f"; {failed} emails FAILED to parse and were not relayed"
    if relay_failed:
        summary += f"; {relay_failed} relay attempts failed"

    notes = ["A lead is a successfully parsed inbound job-notification email."]
    if failed:
        notes.append(
            "Parse failures usually mean the sender changed their email template — those "
            "leads are stored but never relayed to GHL. Inspect them at /api/emails?parse_status=failed."
        )

    return ok(
        summary=summary + ".",
        data={
            "leads": total,
            "unique_job_ids": unique_jobs,
            "relayed_to_ghl": relayed,
            "not_yet_relayed": total - relayed,
            "parse_failed": failed,
            "relay_failed": relay_failed,
            "group_by": group_by,
            "breakdown": breakdown,
        },
        applied_filters={**described, "source": source, "parse_status": "parsed"},
        notes=notes,
    )


# --- messages ------------------------------------------------------------------------
@router.get("/messages/stats")
async def message_stats(
    period: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    direction: str | None = Query(None, description="inbound | outbound"),
    group_by: str = Query("day", description="day | direction | none"),
    db: AsyncSession = Depends(get_db),
    _: AuthedKey = Depends(require_scope(SCOPE_READ)),
) -> dict:
    """SMS/MMS volume on the tracking numbers."""
    start, end, described = _window(period, date_from, date_to)
    where = []
    if start is not None:
        where.append(Message.received_at >= start)
    where.append(Message.received_at < end)
    if direction:
        where.append(Message.direction == direction)

    total = (await db.execute(select(func.count()).select_from(Message).where(*where))).scalar_one()
    inbound = (await db.execute(
        select(func.count()).select_from(Message).where(*where, Message.direction == "inbound")
    )).scalar_one()
    with_media = (await db.execute(
        select(func.count()).select_from(Message).where(*where, Message.num_media > 0)
    )).scalar_one()
    unique_contacts = (await db.execute(
        select(func.count(func.distinct(Message.caller_id))).where(*where, Message.caller_id.is_not(None))
    )).scalar_one()

    breakdown: list[dict] = []
    if group_by == "day":
        day = func.date_trunc("day", func.timezone(settings.BUSINESS_TZ, Message.received_at))
        breakdown = _series((await db.execute(
            select(day.label("d"), func.count(Message.id)).where(*where).group_by("d").order_by("d")
        )).all())
    elif group_by == "direction":
        breakdown = [
            {"direction": d or "(unknown)", "count": c}
            for d, c in (await db.execute(
                select(Message.direction, func.count(Message.id)).where(*where).group_by(Message.direction)
            )).all()
        ]

    window = periods.describe_window(described)
    return ok(
        summary=f"{total} messages {window} ({inbound} inbound, {total - inbound} outbound), "
                f"{unique_contacts} distinct contacts.",
        data={
            "total_messages": total, "inbound": inbound, "outbound": total - inbound,
            "with_media": with_media, "unique_contacts": unique_contacts,
            "group_by": group_by, "breakdown": breakdown,
        },
        applied_filters={**described, "direction": direction},
    )


# --- billing -------------------------------------------------------------------------
@router.get("/billing/summary")
async def billing_summary(
    period: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    group_by: str = Query("day", description="day | number | kind | direction | none"),
    db: AsyncSession = Depends(get_db),
    _: AuthedKey = Depends(require_scope(SCOPE_READ)),
) -> dict:
    """Telephony spend from BulkVS's own rated call records.

    Costs are per LEG, not per call: an inbound call a flow forwards back out is billed twice
    (inbound minutes + outbound minutes), so leg counts here exceed call counts elsewhere by
    design. Recurring charges (DID rental, E911) are not in this feed.
    """
    start, end, described = _window(period, date_from, date_to)
    where = [CallCharge.started_at.is_not(None)]
    if start is not None:
        where.append(CallCharge.started_at >= start)
    where.append(CallCharge.started_at < end)

    total, legs, billed = (await db.execute(
        select(func.coalesce(func.sum(CallCharge.amount), 0), func.count(),
               func.coalesce(func.sum(CallCharge.billed_seconds), 0))
        .select_from(CallCharge).where(*where)
    )).first()
    unrated = (await db.execute(
        select(func.count()).select_from(CallCharge).where(*where, CallCharge.unrated.is_(True))
    )).scalar_one()

    breakdown: list[dict] = []
    if group_by == "day":
        day = func.date_trunc("day", func.timezone(settings.BUSINESS_TZ, CallCharge.started_at))
        breakdown = [
            {"day": d.date().isoformat() if d else None, "amount": float(a or 0), "legs": c}
            for d, a, c in (await db.execute(
                select(day.label("d"), func.sum(CallCharge.amount), func.count())
                .where(*where).group_by("d").order_by("d")
            )).all()
        ]
    elif group_by == "number":
        breakdown = [
            {"number": n or "(unknown)", "amount": float(a or 0), "legs": c}
            for n, a, c in (await db.execute(
                select(Number.phone_number, func.sum(CallCharge.amount), func.count())
                .select_from(CallCharge).join(Number, CallCharge.number_id == Number.id, isouter=True)
                .where(*where).group_by(Number.phone_number)
                .order_by(func.sum(CallCharge.amount).desc()).limit(50)
            )).all()
        ]
    elif group_by in ("kind", "direction"):
        col = CallCharge.kind if group_by == "kind" else CallCharge.direction
        breakdown = [
            {group_by: k or "(unknown)", "amount": float(a or 0), "legs": c}
            for k, a, c in (await db.execute(
                select(col, func.sum(CallCharge.amount), func.count())
                .where(*where).group_by(col).order_by(func.sum(CallCharge.amount).desc())
            )).all()
        ]

    window = periods.describe_window(described)
    notes = ["Usage charges only — recurring DID rental and E911 are not in this feed.",
             "Costs are per billed leg; a forwarded call produces two legs."]
    if unrated:
        notes.append(f"{unrated} legs could not be priced and are recorded at $0 — the real "
                     f"total is higher than shown.")

    return ok(
        summary=f"${float(total or 0):.2f} of telephony usage {window} across {legs} billed legs.",
        data={
            "total_amount": round(float(total or 0), 4),
            "billed_legs": legs,
            "billed_seconds": int(billed or 0),
            "unrated_legs": unrated,
            "group_by": group_by, "breakdown": breakdown,
        },
        applied_filters=described,
        notes=notes,
    )


# --- top callers / analysis mix -------------------------------------------------------
@router.get("/calls/top-callers")
async def top_callers(
    period: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    include_junk: bool = False,
    limit: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: AuthedKey = Depends(require_scope(SCOPE_READ)),
) -> dict:
    """Who called most in the window — repeat callers, and often the noisiest robocallers."""
    start, end, described = _window(period, date_from, date_to)
    where = call_filters(start, end, include_junk)
    rows = (await db.execute(
        select(Caller.phone_number, Caller.label, func.count(Call.id),
               func.max(Call.started_at), func.sum(func.coalesce(Call.duration_seconds, 0)))
        .select_from(Call).join(Caller, Call.caller_id == Caller.id)
        .where(*where).group_by(Caller.phone_number, Caller.label)
        .order_by(func.count(Call.id).desc()).limit(limit)
    )).all()
    items = [
        {"phone": p, "label": lbl, "calls": c,
         "last_call_at": last.isoformat() if last else None,
         "total_duration_seconds": int(dur or 0)}
        for p, lbl, c, last, dur in rows
    ]
    return ok(
        summary=f"Top {len(items)} callers {periods.describe_window(described)}.",
        data={"callers": items},
        applied_filters={**described, "include_junk": include_junk, "limit": limit},
        notes=[NOTE_JUNK_INCLUDED if include_junk else NOTE_JUNK],
    )


@router.get("/calls/categories")
async def call_categories(
    period: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    include_junk: bool = False,
    db: AsyncSession = Depends(get_db),
    _: AuthedKey = Depends(require_scope(SCOPE_READ)),
) -> dict:
    """The LLM's category mix for analyzed calls (human overrides win, as everywhere else).

    Only calls with a recording that was transcribed AND analyzed appear here, so the total
    will be lower than call volume — that gap is reported rather than papered over.
    """
    start, end, described = _window(period, date_from, date_to)
    where = call_filters(start, end, include_junk)
    total = (await db.execute(select(func.count()).select_from(Call).where(*where))).scalar_one()
    category = func.coalesce(CallAnalysis.category_override, CallAnalysis.category)
    rows = (await db.execute(
        select(category.label("c"), func.count(Call.id))
        .select_from(Call).join(CallAnalysis, CallAnalysis.call_id == Call.id)
        .where(*where).group_by("c").order_by(func.count(Call.id).desc())
    )).all()
    analyzed = sum(c for _, c in rows)
    return ok(
        summary=f"{analyzed} of {total} calls {periods.describe_window(described)} have an AI "
                f"category; the rest were never recorded, transcribed or analyzed.",
        data={
            "analyzed_calls": analyzed,
            "total_calls": total,
            "not_analyzed": total - analyzed,
            "categories": [{"category": c or "(none)", "count": n} for c, n in rows],
        },
        applied_filters={**described, "include_junk": include_junk},
        notes=[NOTE_SPAM_DEAD,
               "Categories come from the LLM unless a human overrode them; overrides win."],
    )

"""Campaign attribution observability — STRICTLY READ-ONLY.

This module exists to answer one question: which campaign deserves more money and time?
It joins qualified inbound calls (per campaign) against closed jobs + revenue, which the
operator supplies as a Workiz CSV.

Deliberate design constraints (do not "improve" these away):

  - NOTHING here writes. No INSERT/UPDATE/DELETE, no new table, no migration. The module is
    disconnected from the rest of the platform: it only observes what ingestion already
    recorded. `/match` is a POST purely because it carries a phone-number list too long for
    a query string — it is a SELECT, nothing more.
  - Job/revenue data is NEVER persisted server-side. The frontend parses the Workiz CSV in
    the browser and keeps it in localStorage; only the phone numbers are sent here, to be
    resolved to campaigns. Re-uploading the CSV is how the numbers refresh.
  - "Not spam" is approximated by DURATION, not by call_analysis. As of this writing the
    LLM classifier has analysed 25 of 30,589 calls, so `is_spam` is unusable as a filter —
    a duration floor (default 30s) is the only signal that actually separates real inbound
    leads from robocalls and misdials.
  - Calls with a NULL `started_at` are excluded everywhere. ~25k such rows exist: Twilio
    backfill stubs with no events, no status and no duration. They are not calls.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.config import settings
from app.db import get_db
from app.models import Call, Caller, Campaign, Number, User

router = APIRouter(prefix="/api/attribution", tags=["attribution"])

# Duration floor separating a real lead from a robocall/misdial. The operator can move it;
# 30s is the agreed default (a roofing enquiry that gets past hello runs longer than that).
DEFAULT_MIN_DURATION = 30

UNATTRIBUTED = "(unattributed)"


def _qualified(start: datetime, end: datetime, min_seconds: int) -> list:
    """The one definition of a 'qualified call', shared by every aggregate below.

    Inbound, actually happened (non-NULL started_at), inside the window, and longer than the
    duration floor. Outbound legs are excluded — they are our dials, not campaign leads.
    """
    return [
        Call.started_at.is_not(None),
        Call.started_at >= start,
        Call.started_at < end,
        Call.duration_seconds.is_not(None),
        Call.duration_seconds > min_seconds,
        or_(Call.direction.is_(None), Call.direction != "outbound"),
    ]


class CampaignCallStats(BaseModel):
    campaign: str
    qualified_calls: int
    unique_callers: int
    new_for_campaign: int
    avg_duration_seconds: float | None = None
    first_call_at: datetime | None = None
    last_call_at: datetime | None = None


class MonthlyPoint(BaseModel):
    month: str          # YYYY-MM in business tz
    campaign: str
    qualified_calls: int


class CampaignCallsOut(BaseModel):
    range_from: datetime
    range_to: datetime
    min_duration_seconds: int
    total_qualified_calls: int
    total_calls_in_range: int      # before the duration floor — shows what the floor removes
    campaigns: list[CampaignCallStats]
    monthly: list[MonthlyPoint]
    by_number: list[dict]          # tracking-number detail behind each campaign


class MatchRequest(BaseModel):
    """Phone numbers to resolve to campaigns. E.164, as parsed from the CSV in the browser."""

    phones: list[str] = Field(default_factory=list, max_length=5000)
    min_duration_seconds: int = DEFAULT_MIN_DURATION
    date_from: datetime | None = None
    date_to: datetime | None = None


class PhoneMatch(BaseModel):
    phone: str
    qualified_calls: int
    # First touch is what the module attributes on (the campaign that DISCOVERED the lead).
    # Last touch is returned alongside it so the UI can show when the two disagree rather
    # than silently picking one.
    first_campaign: str | None = None
    first_call_at: datetime | None = None
    last_campaign: str | None = None
    last_call_at: datetime | None = None
    total_talk_seconds: int = 0


class MatchOut(BaseModel):
    range_from: datetime
    range_to: datetime
    min_duration_seconds: int
    requested: int
    matched: int
    matches: list[PhoneMatch]


def _window(date_from: datetime | None, date_to: datetime | None) -> tuple[datetime, datetime]:
    """Default to the trailing 12 months — this module is a year-scale question."""
    now = datetime.now(timezone.utc)
    return (date_from or (now - timedelta(days=365))), (date_to or now)


@router.get("/campaigns", response_model=CampaignCallsOut)
async def campaign_calls(
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    min_duration_seconds: int = DEFAULT_MIN_DURATION,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> CampaignCallsOut:
    """Qualified inbound call volume per campaign. Read-only."""
    start, end = _window(date_from, date_to)
    where = _qualified(start, end, min_duration_seconds)

    # Denominator for "what did the duration floor throw away", using the same window and
    # direction rule but no duration floor at all.
    total_in_range = (await db.execute(
        select(func.count()).select_from(Call).where(
            Call.started_at.is_not(None), Call.started_at >= start, Call.started_at < end,
            or_(Call.direction.is_(None), Call.direction != "outbound"),
        )
    )).scalar_one()

    name = func.coalesce(Campaign.name, UNATTRIBUTED)
    rows = (await db.execute(
        select(
            name.label("campaign"),
            func.count(Call.id),
            func.count(func.distinct(Call.caller_id)),
            func.count(Call.id).filter(Call.is_new_for_campaign.is_(True)),
            func.avg(Call.duration_seconds),
            func.min(Call.started_at),
            func.max(Call.started_at),
        )
        .join(Campaign, Call.campaign_id == Campaign.id, isouter=True)
        .where(*where)
        .group_by(name)
        .order_by(func.count(Call.id).desc())
    )).all()

    campaigns = [
        CampaignCallStats(
            campaign=c, qualified_calls=calls, unique_callers=uniq, new_for_campaign=new,
            avg_duration_seconds=float(avg) if avg is not None else None,
            first_call_at=first, last_call_at=last,
        )
        for c, calls, uniq, new, avg, first, last in rows
    ]

    # Monthly series bucketed in the business tz (decision #10 — never UTC months).
    local_ts = func.timezone(settings.BUSINESS_TZ, Call.started_at)
    month = func.date_trunc("month", local_ts)
    monthly = [
        MonthlyPoint(month=m.strftime("%Y-%m"), campaign=c, qualified_calls=n)
        for m, c, n in (await db.execute(
            select(month.label("m"), name.label("campaign"), func.count(Call.id))
            .join(Campaign, Call.campaign_id == Campaign.id, isouter=True)
            .where(*where).group_by("m", "campaign").order_by("m")
        )).all()
        if m is not None
    ]

    by_number = [
        {
            "number": num or "(unknown)",
            "friendly": friendly,
            "campaign": camp or UNATTRIBUTED,
            "qualified_calls": n,
        }
        for num, friendly, camp, n in (await db.execute(
            select(Number.phone_number, Number.friendly_name, Campaign.name, func.count(Call.id))
            .join(Number, Call.number_id == Number.id, isouter=True)
            .join(Campaign, Call.campaign_id == Campaign.id, isouter=True)
            .where(*where)
            .group_by(Number.phone_number, Number.friendly_name, Campaign.name)
            .order_by(func.count(Call.id).desc()).limit(50)
        )).all()
    ]

    return CampaignCallsOut(
        range_from=start, range_to=end, min_duration_seconds=min_duration_seconds,
        total_qualified_calls=sum(c.qualified_calls for c in campaigns),
        total_calls_in_range=total_in_range,
        campaigns=campaigns, monthly=monthly, by_number=by_number,
    )


@router.post("/match", response_model=MatchOut)
async def match_phones(
    body: MatchRequest,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> MatchOut:
    """Resolve caller phone numbers to the campaign that produced them. Read-only.

    POST only because the phone list is too long for a query string — this writes nothing.
    Numbers are matched exactly against `callers.phone_number` (E.164); normalisation is the
    caller's job and happens in the browser when the CSV is parsed.
    """
    start, end = _window(body.date_from, body.date_to)
    phones = sorted({p for p in body.phones if p})
    if not phones:
        return MatchOut(range_from=start, range_to=end,
                        min_duration_seconds=body.min_duration_seconds,
                        requested=0, matched=0, matches=[])

    where = _qualified(start, end, body.min_duration_seconds) + [Caller.phone_number.in_(phones)]

    # Aggregate first, so a phone with no qualified call simply never appears.
    totals = {
        phone: (n, int(talk or 0))
        for phone, n, talk in (await db.execute(
            select(Caller.phone_number, func.count(Call.id), func.sum(Call.duration_seconds))
            .join(Call, Call.caller_id == Caller.id)
            .where(*where).group_by(Caller.phone_number)
        )).all()
    }

    # DISTINCT ON (Postgres): one row per phone, the earliest / latest CAMPAIGN-BEARING call.
    #
    # Restricted to campaign_id IS NOT NULL deliberately. A call that arrived on a tracking
    # number with no campaign carries no channel signal at all, so it must not veto
    # attribution — without this filter a lead whose first call happened to land on an
    # unattributed DID reports first_campaign=None and silently falls through to the Workiz
    # fallback, even when every later call plainly came from a known campaign.
    async def _edge(ascending: bool) -> dict:
        order = Call.started_at.asc() if ascending else Call.started_at.desc()
        rows = (await db.execute(
            select(Caller.phone_number, Campaign.name, Call.started_at)
            .join(Call, Call.caller_id == Caller.id)
            .join(Campaign, Call.campaign_id == Campaign.id)
            .where(*where)
            .distinct(Caller.phone_number)
            .order_by(Caller.phone_number, order)
        )).all()
        return {phone: (camp, ts) for phone, camp, ts in rows}

    first, last = await _edge(True), await _edge(False)

    matches = [
        PhoneMatch(
            phone=phone,
            qualified_calls=totals[phone][0],
            total_talk_seconds=totals[phone][1],
            first_campaign=first.get(phone, (None, None))[0],
            first_call_at=first.get(phone, (None, None))[1],
            last_campaign=last.get(phone, (None, None))[0],
            last_call_at=last.get(phone, (None, None))[1],
        )
        for phone in sorted(totals)
    ]

    return MatchOut(
        range_from=start, range_to=end, min_duration_seconds=body.min_duration_seconds,
        requested=len(phones), matched=len(matches), matches=matches,
    )

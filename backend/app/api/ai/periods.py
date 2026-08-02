"""Named time periods, resolved in the business timezone.

"How many calls today?" is ambiguous until you fix a timezone, and getting it wrong is the
kind of bug that never announces itself — an evening call lands in the wrong day and every
daily number is quietly off by a few. OWEN's dashboard already buckets by `BUSINESS_TZ`
(America/New_York), so the AI API resolves periods the same way. Anything else would make the
two surfaces disagree.

Everything resolves to a half-open [start, end) pair of *aware UTC* datetimes, which is what
the DB columns hold. Half-open is what makes "today" include 23:59:59 without also including
tomorrow's midnight.

Weeks start Monday. Months are calendar months in business time. DST is handled by localizing
the naive local boundary and converting — never by adding 24h, which is wrong twice a year.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.config import settings

PERIODS: dict[str, str] = {
    "today": "Midnight today (business tz) until now.",
    "yesterday": "The whole of the previous calendar day.",
    "last_24h": "A rolling 24 hours ending now.",
    "last_7d": "A rolling 7 days ending now.",
    "last_30d": "A rolling 30 days ending now.",
    "last_90d": "A rolling 90 days ending now.",
    "this_week": "Monday 00:00 of the current week until now.",
    "last_week": "The whole of the previous Monday-Sunday week.",
    "this_month": "The 1st of the current month until now.",
    "last_month": "The whole of the previous calendar month.",
    "mtd": "Alias of this_month.",
    "ytd": "January 1st of the current year until now.",
    "all_time": "No lower bound. Use sparingly - it scans the whole table.",
}

DEFAULT_PERIOD = "last_7d"


def business_tz() -> ZoneInfo:
    return ZoneInfo(settings.BUSINESS_TZ)


def _local_midnight(d: date, tz: ZoneInfo) -> datetime:
    """Start-of-day in business time, as UTC. Localizing the naive boundary (rather than
    doing arithmetic on UTC) is what keeps this correct across DST transitions."""
    return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc)


def resolve(
    period: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime, dict]:
    """Return (start, end, described) — `start` is None only for all_time.

    Explicit date_from/date_to always win over `period`; naive datetimes are interpreted as
    business-local, because a caller who writes "2026-07-01" means the local day, not UTC.
    """
    tz = business_tz()
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = now_utc.astimezone(tz).date()

    if date_from is not None or date_to is not None:
        start = _as_utc(date_from, tz) if date_from is not None else None
        end = _as_utc(date_to, tz) if date_to is not None else now_utc
        return start, end, {
            "period": "custom",
            "from": start.isoformat() if start else None,
            "to": end.isoformat(),
            "timezone": settings.BUSINESS_TZ,
        }

    name = (period or DEFAULT_PERIOD).strip().lower()
    if name == "mtd":
        name = "this_month"
    if name not in PERIODS:
        raise ValueError(name)

    end = now_utc
    if name == "today":
        start = _local_midnight(today, tz)
    elif name == "yesterday":
        start = _local_midnight(today - timedelta(days=1), tz)
        end = _local_midnight(today, tz)
    elif name == "last_24h":
        start = now_utc - timedelta(hours=24)
    elif name == "last_7d":
        start = now_utc - timedelta(days=7)
    elif name == "last_30d":
        start = now_utc - timedelta(days=30)
    elif name == "last_90d":
        start = now_utc - timedelta(days=90)
    elif name == "this_week":
        start = _local_midnight(today - timedelta(days=today.weekday()), tz)
    elif name == "last_week":
        this_monday = today - timedelta(days=today.weekday())
        start = _local_midnight(this_monday - timedelta(days=7), tz)
        end = _local_midnight(this_monday, tz)
    elif name == "this_month":
        start = _local_midnight(today.replace(day=1), tz)
    elif name == "last_month":
        first_this = today.replace(day=1)
        # Step back one day from the 1st to land in the previous month, whatever its length.
        start = _local_midnight((first_this - timedelta(days=1)).replace(day=1), tz)
        end = _local_midnight(first_this, tz)
    elif name == "ytd":
        start = _local_midnight(date(today.year, 1, 1), tz)
    else:  # all_time
        start = None

    return start, end, {
        "period": name,
        "from": start.isoformat() if start else None,
        "to": end.isoformat(),
        "timezone": settings.BUSINESS_TZ,
    }


def _as_utc(dt: datetime, tz: ZoneInfo) -> datetime:
    """Naive input is business-local; aware input is respected as given."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def describe_window(described: dict) -> str:
    """A short human phrase for the `summary` line, e.g. 'Jul 25 - Aug 01 2026 (America/New_York)'.

    Formatted with zero-padded `%d` rather than the `%-d` glibc extension, which raises on
    Windows — the test suite runs on both.
    """
    tz = business_tz()
    to_local = datetime.fromisoformat(described["to"]).astimezone(tz)
    if not described.get("from"):
        return f"all time through {to_local.strftime('%b %d %Y')} ({settings.BUSINESS_TZ})"
    from_local = datetime.fromisoformat(described["from"]).astimezone(tz)
    return (f"{from_local.strftime('%b %d')} - {to_local.strftime('%b %d %Y')} "
            f"({settings.BUSINESS_TZ})")

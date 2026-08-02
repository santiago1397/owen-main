"""What counts as a call.

This is the single most important file in the AI API, because OWEN's `calls` table contains
rows that are not calls:

- **Phantom rows.** Tens of thousands of rows carry no `started_at` — ingestion artifacts from
  provider backfills and leg correlation. `SELECT count(*) FROM calls` returns roughly 30k when
  real call volume is a small fraction of that. The dashboard dodges this only by accident: its
  date bounds implicitly drop NULLs. Here it is explicit and NOT overridable, because there is
  no honest question whose answer includes those rows.

- **Junk.** Calls of <= 13s or that never connected (failed/busy/no-answer/canceled). Real
  events, but not leads. Excluded by default, re-includable with `include_junk=true`, using the
  exact same predicate as `api/junk.py` so the AI API and the dashboard can never disagree.

`api/junk.py` is imported rather than reimplemented for precisely that reason.
"""

from __future__ import annotations

from sqlalchemy import and_

from app.api.junk import IS_JUNK, NOT_JUNK
from app.models import Call

# Non-overridable. A row with no start time is not a call that happened.
REAL_CALL = Call.started_at.is_not(None)

__all__ = ["REAL_CALL", "IS_JUNK", "NOT_JUNK", "call_filters"]


def call_filters(
    start=None,
    end=None,
    include_junk: bool = False,
    min_duration: int | None = None,
    max_duration: int | None = None,
    campaign_id=None,
    number_id=None,
    direction: str | None = None,
    status: str | None = None,
    answered: bool | None = None,
    new_callers: bool | None = None,
) -> list:
    """Build the WHERE list shared by every call metric.

    Duration bounds are inclusive on both ends (`max_duration=45` means "45 seconds or less",
    which is what a person asking "under 45 seconds" means in practice) and always exclude
    NULL durations, since an unknown duration can't be known to be under a threshold.
    """
    where = [REAL_CALL]
    if start is not None:
        where.append(Call.started_at >= start)
    if end is not None:
        where.append(Call.started_at < end)  # half-open
    if not include_junk:
        where.append(NOT_JUNK)
    if min_duration is not None:
        where.append(and_(Call.duration_seconds.is_not(None), Call.duration_seconds >= min_duration))
    if max_duration is not None:
        where.append(and_(Call.duration_seconds.is_not(None), Call.duration_seconds <= max_duration))
    if campaign_id is not None:
        where.append(Call.campaign_id == campaign_id)
    if number_id is not None:
        where.append(Call.number_id == number_id)
    if direction:
        where.append(Call.direction == direction)
    if status:
        where.append(Call.status == status)
    if answered is not None:
        # "Answered" means the provider gave us an answer timestamp — stronger and more
        # reliable than reading the status string, which varies by provider.
        where.append(Call.answered_at.is_not(None) if answered else Call.answered_at.is_(None))
    if new_callers is not None:
        where.append(Call.is_new_for_campaign.is_(new_callers))
    return where

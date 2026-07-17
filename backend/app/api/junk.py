"""Shared "likely junk" call predicate, used by both the dashboard and the calls list so
the two views agree on what counts as junk.

A call is likely junk if it lasted <= JUNK_CALL_MAX_DURATION_SECONDS OR it never connected
(status in JUNK_STATUSES). NULL duration / NULL status count as NOT junk — the call may
still be in flight and we never want to silently hide a live call.
"""

from sqlalchemy import and_, or_

from app.api.deps import JUNK_CALL_MAX_DURATION_SECONDS, JUNK_STATUSES
from app.models import Call

IS_JUNK = or_(
    and_(Call.duration_seconds.is_not(None), Call.duration_seconds <= JUNK_CALL_MAX_DURATION_SECONDS),
    and_(Call.status.is_not(None), Call.status.in_(JUNK_STATUSES)),
)

# NULL-safe negation: a plain not_(IS_JUNK) would drop NULL-status rows (three-valued
# logic), so spell out "not junk" keeping NULLs on the non-junk side.
NOT_JUNK = and_(
    or_(Call.duration_seconds.is_(None), Call.duration_seconds > JUNK_CALL_MAX_DURATION_SECONDS),
    or_(Call.status.is_(None), Call.status.notin_(JUNK_STATUSES)),
)

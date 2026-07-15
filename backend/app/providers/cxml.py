"""Shared cXML (Twilio / SignalWire Compatibility API) helpers used by reconciliation.
Both providers return the same Call resource shape, so normalization is shared.
"""

from datetime import datetime
from email.utils import parsedate_to_datetime

from app.providers.base import NormalizedCallEvent


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def to_int(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def normalize_call(call: dict) -> NormalizedCallEvent:
    status = call.get("status")
    return NormalizedCallEvent(
        provider_call_sid=call.get("sid", ""),
        event_type=status or "reconciled",
        status=status,
        from_number=call.get("from"),
        to_number=call.get("to"),
        direction=call.get("direction"),
        started_at=parse_dt(call.get("start_time")),
        ended_at=parse_dt(call.get("end_time")),
        duration_seconds=to_int(call.get("duration")),
        provider_sequence=f"{call.get('sid', '')}:{status}",
        raw=call,
    )

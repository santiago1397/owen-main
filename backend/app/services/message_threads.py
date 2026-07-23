"""Pure thread-grouping for the Messages inbox (Ticket 09).

A thread is DERIVED from the `messages` table by (number_id, caller_id) — there is no thread
table and no stored read-state. The API fetches the message rows newest-first and hands them
here; this collapses them into one summary per conversation (the first row seen for a key is
therefore the latest message). Kept import-light (stdlib only) so it's unit-testable without
sqlalchemy / a database.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class ThreadSummary:
    number_id: str | None
    caller_id: str | None
    caller_number: str | None
    number_phone: str | None
    number_label: str | None
    campaign_name: str | None
    provider: str | None
    last_body: str | None
    last_direction: str | None
    last_at: datetime | None
    message_count: int


def group_threads(rows) -> list[ThreadSummary]:
    """Collapse newest-first message rows into per-(number_id, caller_id) thread summaries,
    preserving newest-thread-first order. `rows` are duck-typed (any object exposing the
    message/attribution attributes below)."""
    threads: dict[tuple, ThreadSummary] = {}
    order: list[tuple] = []
    for r in rows:
        key = (
            str(r.number_id) if r.number_id is not None else None,
            str(r.caller_id) if r.caller_id is not None else None,
        )
        if key not in threads:
            threads[key] = ThreadSummary(
                number_id=key[0],
                caller_id=key[1],
                caller_number=getattr(r, "caller_number", None),
                number_phone=getattr(r, "number_phone", None),
                number_label=getattr(r, "number_label", None),
                campaign_name=getattr(r, "campaign_name", None),
                provider=getattr(r, "provider", None),
                last_body=r.body,
                last_direction=getattr(r, "direction", None),
                last_at=getattr(r, "received_at", None),
                message_count=1,
            )
            order.append(key)
        else:
            threads[key].message_count += 1
    return [threads[k] for k in order]

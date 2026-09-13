"""The `openphone_mirror_rows` table — WHAT has already been mirrored into the CRM.

One row per OpenPhone object (call or text) this module has handed to the CRM. It exists
for exactly one reason, and it is the reason that makes a poll safe to re-run:

    OpenPhone's `/calls` has no time-based sweep (spec D11a). Ingestion is participant-
    driven, which means every poll re-reads the SAME recent calls for every participant it
    asks about. Without a record of what has been sent, a five-minute poll would re-push a
    day's calls 288 times.

## Why a table and not a cache

Because the guarantee has to survive a worker restart, a redeploy and a second backfill.
An in-process set is empty after any of those, and the first poll afterwards would re-push
the entire window onto real customers' timelines.

## This is the CHEAP half of idempotency, not the guarantee

It stops the duplicate work. It does NOT, on its own, stop a duplicate ROW, because the
gap it cannot close is the one between "the CRM committed the event" and "the CRM's 201
reached us" — a worker timeout in that window retries a job the CRM has already applied.

The guarantee is on the CRM side: `conversation_events.dedupe_key` is UNIQUE, and
`POST /api/events` returns the existing row rather than inserting a second one. This table
means we rarely ask; that column means it does not matter when we do.

Deliberately NOT columns on `calls` or `messages`: those are the platform's own tables,
written by ingestion and read by billing, the Inbox and the flow runtime. The reasoning is
`integrations/crm/models.py`'s, verbatim — an integration must be removable in one
migration and invisible to everything that does not join to it. Nothing in the platform
joins to this table; a `DROP TABLE openphone_mirror_rows` turns the mirror off and changes
no other behaviour.

No customer content is stored here. The body of a text and the transcript of a call pass
THROUGH this module to the CRM, which is the system of record for them; keeping a second
copy in OWEN's database would put customer correspondence in `pg_dump` and within reach of
the `owen_ro` role behind `/api/ai/query` for no benefit at all.
"""

import uuid
from datetime import datetime

from sqlalchemy import (DateTime, Index, Integer, String, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# The key under which `sync.py` records that the one-time backfill has run, in the existing
# `app_settings` table. A second table for one boolean would be a migration nobody needs.
BACKFILL_SETTING_KEY = "openphone_mirror_backfill"


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class OpenPhoneMirrorRow(Base):
    """One OpenPhone object already delivered to the CRM."""

    __tablename__ = "openphone_mirror_rows"
    __table_args__ = (
        # THE constraint this table exists for. Two poll ticks racing on the same call both
        # try to insert; the loser gets an IntegrityError and skips, which is the correct
        # outcome and is why `sync.py` inserts BEFORE it enqueues rather than after.
        UniqueConstraint("kind", "external_id", name="uq_openphone_mirror_object"),
        # "What has this mirror done lately?" — the operator question behind `manage.py
        # status`, and the query a backfill runs to find where it got to.
        Index("ix_openphone_mirror_occurred", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True,
                                          default=_uuid)

    # "call" | "message". A plain string rather than an enum: adding a third kind later
    # should not be a migration on a live phone system.
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # OpenPhone's own immutable id for the object. Half of the unique key above.
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)

    # Exactly the string sent to the CRM as `dedupe_key`, stored so an operator debugging a
    # duplicate can grep one value across two databases instead of re-deriving it.
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)

    # Last ten digits of the customer's number — the identity rule both systems share. The
    # full number is not stored: this table answers "have we sent this?", and it can do that
    # with a match key. See the docstring on not keeping a second copy of customer data.
    customer_key: Mapped[str | None] = mapped_column(String(20), index=True)
    # WHICH OpenPhone line it came through. Ours, not a customer's, so it is stored in full.
    line_number: Mapped[str | None] = mapped_column(String(40))

    # When OpenPhone says it happened. Drives the backfill's "how far back have I got?".
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When WE queued it for the CRM. Different from the above by up to the whole window on a
    # backfill, and the difference is the thing an operator wants to see when the mirror
    # looks stuck.
    pushed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # How many times we have enqueued it. Stays 1 in normal running; a number above 1 means
    # a delivery failed and was retried, which is the signal that the CRM-side unique
    # constraint is doing real work rather than being decorative.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

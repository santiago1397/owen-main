"""Phase 1 schema. See ARCHITECTURE.md for the reasoning behind each choice.

Notes that encode design decisions:
- `call_events` is the append-only source of truth; `calls` is a projection.
- `calls` has a unique (provider_id, provider_call_sid) so webhook retries are idempotent.
- `campaign_id` is stamped onto `calls` at ingest so historical reports never re-attribute.
- `is_new_for_campaign` = per-campaign new caller; global new comes from callers.first_seen_at.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)  # 'twilio' | 'signalwire'
    account_ref: Mapped[str | None] = mapped_column(String)  # account_sid / project_id (not the secret)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String)
    source: Mapped[str | None] = mapped_column(String)  # craigslist / facebook / ...
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Number(Base):
    __tablename__ = "numbers"
    __table_args__ = (UniqueConstraint("provider_id", "phone_number", name="uq_number_per_provider"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"))
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("campaigns.id"))
    phone_number: Mapped[str] = mapped_column(String, index=True)  # E.164
    friendly_name: Mapped[str | None] = mapped_column(String)
    forwards_to: Mapped[str | None] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Caller(Base):
    __tablename__ = "callers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    phone_number: Mapped[str] = mapped_column(String, unique=True, index=True)  # E.164
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    total_calls: Mapped[int] = mapped_column(Integer, default=0)
    spam_score: Mapped[float | None] = mapped_column(Numeric)  # from transcript analysis (Phase 6)
    spam_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    label: Mapped[str | None] = mapped_column(String)  # manual override


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (
        UniqueConstraint("provider_id", "provider_call_sid", name="uq_call_provider_sid"),
        # Aggregation-path indexes (ARCHITECTURE.md): dashboards group by number/campaign over time.
        Index("ix_calls_number_started", "number_id", "started_at"),
        Index("ix_calls_campaign_started", "campaign_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"), index=True)
    provider_call_sid: Mapped[str] = mapped_column(String)
    number_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("numbers.id"), index=True)
    caller_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("callers.id"), index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("campaigns.id"), index=True)

    direction: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String)  # projection of highest-rank event seen
    status_rank: Mapped[int] = mapped_column(Integer, default=0)  # guards out-of-order updates

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    forwarded_to: Mapped[str | None] = mapped_column(String)
    is_new_for_campaign: Mapped[bool | None] = mapped_column(Boolean)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)

    events: Mapped[list["CallEvent"]] = relationship(back_populates="call")


class CallEvent(Base):
    """Append-only log of every status callback. Source of truth; `calls` is derived from this."""

    __tablename__ = "call_events"
    __table_args__ = (
        UniqueConstraint("call_id", "event_type", "provider_sequence", name="uq_event_natural"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id"), index=True)
    event_type: Mapped[str] = mapped_column(String)
    provider_sequence: Mapped[str | None] = mapped_column(String)  # provider timestamp/seq for dedup
    payload: Mapped[dict | None] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    call: Mapped["Call"] = relationship(back_populates="events")


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id"), index=True)
    provider_recording_sid: Mapped[str] = mapped_column(String, unique=True)  # idempotency key
    status: Mapped[str | None] = mapped_column(String)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    storage_path: Mapped[str | None] = mapped_column(String)  # local disk path (Phase 2)
    provider_url: Mapped[str | None] = mapped_column(String)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transcribed: Mapped[bool] = mapped_column(Boolean, default=False)  # retention gate


class Transcription(Base):
    """Text transcript of a recording (Phase 6). Kept forever — outlives the audio,
    which the retention sweep deletes once `Recording.transcribed` is set.
    """

    __tablename__ = "transcriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id"), index=True)
    recording_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("recordings.id"))
    engine: Mapped[str] = mapped_column(String)  # which STT engine produced it
    text: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String)
    confidence: Mapped[float | None] = mapped_column(Numeric)
    words: Mapped[dict | None] = mapped_column(JSONB)  # timestamped words, if the engine gives them
    status: Mapped[str] = mapped_column(String, default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CallAnalysis(Base):
    """LLM classification over the transcript (Phase 6): spam + category + tags + summary.
    `*_override` columns hold human corrections that win over the model (decision #5, #11).
    """

    __tablename__ = "call_analysis"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id"), unique=True, index=True)
    is_spam: Mapped[bool | None] = mapped_column(Boolean)
    spam_confidence: Mapped[float | None] = mapped_column(Numeric)
    category: Mapped[str | None] = mapped_column(String, index=True)  # controlled enum, chartable
    tags: Mapped[dict | None] = mapped_column(JSONB)  # free-form descriptive list
    summary: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # human overrides
    category_override: Mapped[str | None] = mapped_column(String)
    is_spam_override: Mapped[bool | None] = mapped_column(Boolean)


class Job(Base):
    """Durable Postgres-backed queue. Drained with SELECT ... FOR UPDATE SKIP LOCKED."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    type: Mapped[str] = mapped_column(String, index=True)  # 'recording_fetch' | 'reconcile' | ...
    payload: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)  # pending/running/done/failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default="admin")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

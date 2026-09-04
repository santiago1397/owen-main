"""Phase 1 schema. See ARCHITECTURE.md for the reasoning behind each choice.

Notes that encode design decisions:
- `call_events` is the append-only source of truth; `calls` is a projection.
- `calls` has a unique (provider_id, provider_call_sid) so webhook retries are idempotent.
- `campaign_id` is stamped onto `calls` at ingest so historical reports never re-attribute.
- `is_new_for_campaign` = per-campaign new caller; global new comes from callers.first_seen_at.
"""

import uuid
from datetime import date as dt_date
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy import true as sa_true
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

    # --- BulkVS + Asterisk platform (Ticket 03, additive) ----------------------------
    # Split identity: who OWNS the DID (the carrier the number is registered with, e.g.
    # "bulkvs") vs which provider carries its MEDIA (e.g. "asterisk"). Legacy Twilio/
    # SignalWire numbers leave both NULL and keep attributing by `provider_id` unchanged;
    # BulkVS-synced DIDs stamp owner_provider="bulkvs" + media_provider="asterisk". A later
    # ticket keys media attribution on (media_provider, to_number) — owner is number-only.
    owner_provider: Mapped[str | None] = mapped_column(String)
    media_provider: Mapped[str | None] = mapped_column(String)

    # Soft-release marker for the add-only BulkVS sync: a DID that VANISHES from /tnRecord
    # is soft-released (active=False + released_at set) — its history is frozen, the row is
    # never deleted — and REACTIVATES the same row (released_at cleared, active=True) if it
    # reappears. NULL means the number has never been released.
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Carrier-reported provisioning status, mirrored verbatim from BulkVS /tnRecord's
    # `Status` field on every sync ("Active", or e.g. "SUBMITTED" for a pending port-in).
    # NULL for legacy Twilio/SignalWire rows (treated as active — never locked out).
    # Every operation gate (outbound calls, SMS, inbox default/send) refuses a DID whose
    # status isn't Active via services.number_sync.is_carrier_active.
    provider_status: Mapped[str | None] = mapped_column(String)

    # Optional behaviour assignment: the call-flow this number routes to (assignment is a
    # LATER ticket; the column exists now so derived lifecycle can key on it). NULL until
    # a flow is assigned. Lifecycle (available / pending / assigned / released) is DERIVED
    # from active + released_at + provider_status + whether campaign_id/flow_id is set —
    # lifecycle itself is never stored.
    flow_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("flows.id"))

    # --- Manual outbound SMS gate (Ticket 10, additive) ------------------------------
    # BulkVS outbound SMS requires 10DLC brand+campaign registration (a manual HITL step).
    # Sending is REFUSED unless a number is `sms_enabled` AND has an `sms_campaign_id`
    # (the 10DLC campaign id, entered manually once the carrier approves it). Both stay
    # unset by default so no number can send until an operator explicitly enables it.
    sms_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    sms_campaign_id: Mapped[str | None] = mapped_column(String)

    # --- BulkVS /tnRecord "TN Details", mirrored by the sync (Billing) -----------------
    # `tier` selects the inbound per-minute rate and is the single most cost-sensitive
    # field on this row: the published tiers span $0.0003 to $0.0198 per minute. A DID with
    # no tier is never guessed at — its legs are recorded as `unrated`.
    tier: Mapped[str | None] = mapped_column(String)
    state: Mapped[str | None] = mapped_column(String)
    rate_center: Mapped[str | None] = mapped_column(String)
    activation_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Carrier-side CNAM delivery. When on, BulkVS performs (and bills) a lookup per inbound
    # call, which at this account's rates is a larger line than the minutes themselves.
    cnam_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # True when the DID arrived by PORT rather than purchase (mirrored from BulkVS /portTn).
    # US ports are free; a purchased number pays a $0.05 setup fee — so billing must not
    # charge setup on a ported DID.
    ported_in: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # E911 is NOT reported by /tnRecord and no BulkVS endpoint exposes it, so it is operator-
    # set rather than synced. Defaults off.
    e911_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class Caller(Base):
    __tablename__ = "callers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    phone_number: Mapped[str] = mapped_column(String, unique=True, index=True)  # E.164
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    total_calls: Mapped[int] = mapped_column(Integer, default=0)
    spam_score: Mapped[float | None] = mapped_column(Numeric)  # from transcript analysis (Phase 6)
    spam_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    label: Mapped[str | None] = mapped_column(String)  # manual override / display name (Inbox)
    # Inbox contact panel (Quo-style inbox): free-form CRM-ish fields, operator-edited.
    company: Mapped[str | None] = mapped_column(String)
    role: Mapped[str | None] = mapped_column(String)


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
    # Flow version the ARI interpreter ran for this call (Ticket 07), pinned once at
    # StasisStart exactly like campaign_id is pinned at ingest, so downstream projection /
    # analysis can attribute which graph version handled the call. NULL for calls that ran
    # no assigned flow (all legacy Twilio/SignalWire calls, and unassigned Asterisk DIDs).
    flow_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("flow_versions.id"), index=True
    )
    # Agent version an `ai_agent` flow node ran for this call (Ticket 11), pinned once on node
    # entry exactly like flow_version_id — so analysis can attribute which agent config
    # handled the call. NULL for calls that hit no ai_agent node.
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_versions.id"), index=True
    )

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

    # Completed-call relay to GHL (mirrors Message.relayed_to_ghl). Relay-once guard.
    relayed_to_ghl: Mapped[bool] = mapped_column(Boolean, default=False)
    relayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    # Speaker-separated segments from dual-channel recordings (Q17): ordered list of
    # {speaker: 'caller'|'operator', start: float, end: float, text: str}. NULL for mono
    # recordings (Twilio / pre-stereo / split-failure fallback) — `text` alone is used then.
    segments: Mapped[list | None] = mapped_column(JSONB)
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


class Message(Base):
    """Inbound SMS received on a tracking number, attributed like a call and relayed to
    GHL. Idempotent on provider_message_sid (mirrors Recording.provider_recording_sid).
    """

    __tablename__ = "messages"
    __table_args__ = (
        # Dashboards may group messages by number/campaign over time, like calls.
        Index("ix_messages_number_received", "number_id", "received_at"),
        Index("ix_messages_campaign_received", "campaign_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    provider_id: Mapped[int] = mapped_column(ForeignKey("providers.id"), index=True)
    provider_message_sid: Mapped[str] = mapped_column(String, unique=True)  # idempotency key
    number_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("numbers.id"), index=True)
    caller_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("callers.id"), index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("campaigns.id"), index=True)

    direction: Mapped[str | None] = mapped_column(String)  # 'inbound' | 'outbound' (Ticket 10)
    from_number: Mapped[str | None] = mapped_column(String)
    to_number: Mapped[str | None] = mapped_column(String)
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String)
    num_media: Mapped[int] = mapped_column(Integer, default=0)
    media_urls: Mapped[dict | None] = mapped_column(JSONB)  # list of MMS media URLs

    # Manual outbound audit (Ticket 10): the operator who sent an outbound reply. NULL for all
    # inbound rows (which carry no operator).
    sent_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)

    relayed_to_ghl: Mapped[bool] = mapped_column(Boolean, default=False)  # relay-once guard
    relayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload: Mapped[dict | None] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SmsOptOut(Base):
    """App-level SMS opt-out state per (number_id, contact) — Ticket 10.

    STOP/START/HELP keywords on the INBOUND path maintain a row here; an outbound send to an
    `opted_out` contact is BLOCKED. Absence of a row means the contact never sent a control
    keyword and is allowed. State is the single source of truth for the send gate — HELP does
    not change state. `contact` is the external party's E.164 number (NOT our DID)."""

    __tablename__ = "sms_opt_outs"
    __table_args__ = (
        UniqueConstraint("number_id", "contact", name="uq_optout_number_contact"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    number_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("numbers.id"), index=True)
    contact: Mapped[str] = mapped_column(String)  # external party's E.164 number
    state: Mapped[str] = mapped_column(String)  # 'opted_out' | 'opted_in'
    last_keyword: Mapped[str | None] = mapped_column(String)  # 'stop' | 'start' (audit)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContactThreadState(Base):
    """Per-CONTACT inbox state (Quo-style Inbox): one row per caller, shared by all users
    (global read/open state — single-operator reality). Only user ACTIONS write here
    (open thread => last_read_at, ✓ => closed_at); "unread" and "auto-reopen on new
    activity" are DERIVED against these timestamps (services/inbox_threads.py), so the
    ingestion webhooks never touch this table. Absence of a row = never read, never closed.
    """

    __tablename__ = "contact_thread_state"

    caller_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("callers.id"), primary_key=True
    )
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Quo-style right-click actions. `deleted_at` soft-hides the thread but AUTO-REAPPEARS on
    # activity newer than it (same derived pattern as closed_at). `blocked_at` hides the thread
    # AND gates outbound (send + call refuse a blocked contact); it does NOT auto-reappear.
    # Inbound is store-but-hide — ingestion still records the row, the thread just stays hidden.
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContactNote(Base):
    """Free-form timestamped note on a contact (Inbox contact panel). Append + delete only."""

    __tablename__ = "contact_notes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    caller_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("callers.id"), index=True)
    body: Mapped[str] = mapped_column(Text)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppSetting(Base):
    """Tiny global key/value store for operator-editable app settings (first use: the
    Inbox default outbound DID, key 'inbox_default_number_id'). JSONB value so future
    settings need no migration."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class InboundEmail(Base):
    """A job-notification email pulled from the Hostinger mailbox over IMAP (e.g. Dispatch /
    American Home Shield). Idempotent on the RFC Message-ID (mirrors Message.provider_message_sid
    / Recording.provider_recording_sid). The full raw email is ALWAYS stored; `fields` holds the
    parsed structured data. `parse_status` gates the GHL relay — only 'parsed' rows are sent;
    'failed' rows are kept + flagged for a human to inspect and are never relayed.
    """

    __tablename__ = "inbound_emails"
    __table_args__ = (
        Index("ix_inbound_emails_source_received", "source", "received_at"),
        Index("ix_inbound_emails_parse_status", "parse_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    message_id: Mapped[str] = mapped_column(String, unique=True)  # RFC Message-ID = idempotency key
    source: Mapped[str | None] = mapped_column(String)  # matched sender key, e.g. 'dispatch'
    from_addr: Mapped[str | None] = mapped_column(String)
    to_addr: Mapped[str | None] = mapped_column(String)
    subject: Mapped[str | None] = mapped_column(String)
    job_id: Mapped[str | None] = mapped_column(String, index=True)  # extracted natural key (e.g. AHS work-order #)

    # 'parsed'  — a work order we fully read; this is the only value that relays.
    # 'failed'   — a work order we could NOT read. A real problem; a lead was lost.
    # 'ignored'  — not a work order at all (cancellation, note, account mail). Not an error.
    parse_status: Mapped[str] = mapped_column(String, default="failed")
    parse_error: Mapped[str | None] = mapped_column(Text)  # why parsing failed / which fields were missing
    fields: Mapped[dict | None] = mapped_column(JSONB)  # extracted structured data (what we relay)
    raw: Mapped[str | None] = mapped_column(Text)  # full raw RFC822 email, always kept

    relayed_to_ghl: Mapped[bool] = mapped_column(Boolean, default=False)  # relay-once guard (True only on actual send)
    relayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # last relay attempt time
    # Truthful relay outcome for the log UI: NULL (parsed, not attempted yet) | 'sent' |
    # 'skipped_not_configured' (parsed but GHL_EMAIL_WEBHOOK_URL unset) | 'skipped_not_parsed' |
    # 'failed' (POST errored). 'sent' is the only status that sets relayed_to_ghl.
    relay_status: Mapped[str | None] = mapped_column(String)
    relay_error: Mapped[str | None] = mapped_column(Text)
    # What the relay created in GHL (for the log UI): {mode, contact_id, opportunity_id, ...}.
    relay_result: Mapped[dict | None] = mapped_column(JSONB)

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


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


class Flow(Base):
    """Logical call-flow (name + pointer to its currently active version). Part of the
    BulkVS+Asterisk platform effort. The row is an append-only ENVELOPE: only the
    `active_version_id` pointer is ever mutated (on activation) — the graph itself lives
    in immutable `flow_versions` rows. A later ticket's ARI interpreter pins a call to a
    `flow_version_id` at StasisStart, exactly like `campaign_id` is pinned at ingest.
    """

    __tablename__ = "flows"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String)
    # Pointer to the active version (nullable until first activation). use_alter breaks the
    # flows <-> flow_versions circular FK at DDL time.
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("flow_versions.id", use_alter=True, name="fk_flows_active_version"),
    )
    # Soft delete. A flow can rarely be hard-deleted: calls.flow_version_id references this
    # flow's versions for attribution and every FK in the chain is NO ACTION, so removing a
    # flow that ever handled a call would either violate the constraint or shred call
    # history. Archiving hides it from the library + the Numbers picker instead, leaving
    # every reference intact. See DELETE /api/flows/{id}.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FlowVersion(Base):
    """An immutable snapshot of a flow's directed graph. APPEND-ONLY: every save inserts a
    new row (version = prior max + 1); existing rows are never updated. `graph` holds the
    whole graph — a `nodes` object map keyed by node id, with edges in each node's `next`
    map keyed by port (see app/flows/validator.py). Validation gates ACTIVATION, not saving.
    """

    __tablename__ = "flow_versions"
    __table_args__ = (
        UniqueConstraint("flow_id", "version", name="uq_flow_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    flow_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("flows.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)  # 1-based, monotonically increasing per flow
    graph: Mapped[dict] = mapped_column(JSONB)  # nodes + edges (per-node `next` port map)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Agent(Base):
    """A reusable AI voice agent (name + pointer to its currently active version). Ticket 11,
    mirrors `Flow`: the row is an append-only ENVELOPE — only the `active_version_id` pointer
    is mutated (on activation). Config lives in immutable `agent_versions` rows. An agent is
    NEVER bound to a number; it is only REFERENCED from a flow's `ai_agent` node, and the
    interpreter PINS the specific `agent_version_id` onto the call on node entry (like flows
    pin `flow_version_id`).
    """

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String)
    # Pointer to the active version (nullable until first activation). use_alter breaks the
    # agents <-> agent_versions circular FK at DDL time (same trick as flows).
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_versions.id", use_alter=True, name="fk_agents_active_version"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentVersion(Base):
    """An immutable snapshot of an agent's config. APPEND-ONLY: every save inserts a new row
    (version = prior max + 1); existing rows are never updated. `config` (jsonb) holds
    persona / voice / greeting / model / engine / tools[] toggles / in-context knowledge /
    guardrails (max_call_seconds, max_silence_seconds, model tier). Validation gates
    ACTIVATION, not saving (mirrors flow_versions)."""

    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)  # 1-based, monotonically increasing per agent
    config: Mapped[dict] = mapped_column(JSONB)  # persona/voice/greeting/model/engine/tools/…
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CallCapture(Base):
    """Structured data an AI agent collected DURING a call (AI_AGENT_SPEC D7).

    Append-only, one row per capture event — an agent may capture a name early and the
    problem details later, and both matter with their own timestamps.

    Field vocabulary is a SHARED CORE plus per-agent extras, the same split `call_analysis`
    already uses (a controlled `category` alongside free-form `tags`): the core keys make
    "how many emergency roof leaks did the army capture this month" answerable across every
    agent, while `extra` lets a specialist record something nobody else needs.

    A capture NEVER overwrites a human-entered field. Promoting one onto a `caller` is an
    operator action, not an automatic consequence — humans win over models here as everywhere.
    """

    __tablename__ = "call_captures"
    __table_args__ = (
        Index("ix_call_captures_call", "call_id"),
        Index("ix_call_captures_agent_version", "agent_version_id"),
        Index("ix_call_captures_captured_at", "captured_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id"))
    # Which agent CONFIG produced this — free from version pinning, and the difference
    # between "the army is fine" and "v3 regressed" being a query or an investigation.
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_versions.id")
    )
    capture_type: Mapped[str] = mapped_column(String, default="lead", server_default="lead")
    fields: Mapped[dict | None] = mapped_column(JSONB)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Relay-once guard, same shape as messages/inbound_emails/calls, so captures ride the
    # EXISTING call_relay_ghl handler rather than needing a new relay path.
    relayed_to_ghl: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    relayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BillingRate(Base):
    """The BulkVS price sheet, as data. Seeded from app.services.billing.SEED_RATES.

    `source` is provenance, and it matters: 'sheet' rows were read off the operator's own
    BulkVS portal price table (authoritative for this account), 'web' rows fill gaps the
    portal only linked out to. Keeping the distinction visible means a surprising total can
    be traced to a rate nobody ever verified.
    """

    __tablename__ = "billing_rates"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String)
    unit: Mapped[str] = mapped_column(String)  # per_minute | per_event | per_month
    amount: Mapped[float] = mapped_column(Numeric(12, 6))
    # Per-minute rows only. Stored per-rate so one invoice check can correct the increment
    # without a deploy, and so already-costed legs keep the increment they were billed under.
    increment_seconds: Mapped[int | None] = mapped_column(Integer)
    minimum_seconds: Mapped[int | None] = mapped_column(Integer)
    lnp_fee: Mapped[float | None] = mapped_column(Numeric(12, 4))
    source: Mapped[str] = mapped_column(String, default="sheet", server_default="sheet")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CallCharge(Base):
    """One BulkVS-billable LEG (per charge kind) — the unit BulkVS actually meters.

    Why legs and not calls: an inbound call a flow forwards back out over the same trunk is
    billed TWICE (inbound minutes + outbound minutes). `calls` collapses every leg onto one
    row keyed by Linkedid, so costing from it would understate a forwarded call by ~half.

    The rate is STAMPED here at costing time (rate_code + rate_amount + increment_seconds),
    mirroring how campaign_id is stamped onto calls at ingest: re-pricing later must never
    silently rewrite what a past month cost.
    """

    __tablename__ = "call_charges"
    __table_args__ = (
        # Idempotency: re-running the reconciler can never double-bill a leg.
        UniqueConstraint("uniqueid", "kind", name="uq_charge_leg_kind"),
        Index("ix_call_charges_number_started", "number_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    uniqueid: Mapped[str] = mapped_column(String)          # Asterisk CDR leg id
    linkedid: Mapped[str] = mapped_column(String, index=True)  # groups the legs of one call
    kind: Mapped[str] = mapped_column(String, default="minutes", server_default="minutes")
    call_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("calls.id"))
    number_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("numbers.id"))
    direction: Mapped[str] = mapped_column(String)         # inbound | outbound
    channel: Mapped[str | None] = mapped_column(String)
    src: Mapped[str | None] = mapped_column(String)
    dst: Mapped[str | None] = mapped_column(String)
    # CDR records dst='s' (a dialplan exten) on the flow-dial leg, so a forwarded call's real
    # destination isn't recoverable from CDR alone.
    dest_unknown: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    raw_billsec: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    billed_seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rate_code: Mapped[str | None] = mapped_column(String)
    rate_amount: Mapped[float | None] = mapped_column(Numeric(12, 6))
    increment_seconds: Mapped[int | None] = mapped_column(Integer)
    amount: Mapped[float] = mapped_column(Numeric(12, 6), default=0, server_default="0")
    # Recorded at 0 but FLAGGED — an unpriceable leg is surfaced, never silently free.
    unrated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    unrated_reason: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BillingAdjustment(Base):
    """A manual account-level charge with no call data behind it — LNP port fees, E911
    overage, LIDB updates, directory listings. These are account events, not call events, so
    they cannot be derived from CDR; entering them by hand keeps the monthly total complete
    without inventing anything."""

    __tablename__ = "billing_adjustments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    occurred_on: Mapped[dt_date] = mapped_column(Date, index=True)
    code: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String)
    amount: Mapped[float] = mapped_column(Numeric(12, 4))
    number_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("numbers.id"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default="admin")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiKey(Base):
    """A machine credential for the AI API (`/api/ai/*`). Never a user session.

    The plaintext key is shown exactly once at creation and never stored. `key_hash` is a
    plain SHA-256 (not argon2 like `users.password_hash`): the secret is 32 bytes of CSPRNG
    entropy, so there is nothing to brute-force, and this is verified on EVERY request —
    a deliberate KDF would put ~100ms of argon2 on the hot path for no security gain.

    `key_prefix` is the first few characters of the plaintext, stored in the clear purely so
    the UI can show which key a row refers to.

    Scopes are read-only capabilities, stored as a JSON list (see app/core/apikeys.py):
      read    — curated metrics: counts, durations, series, pipeline health
      content — transcripts, call summaries, SMS bodies, customer PII from parsed emails
      sql     — the guarded read-only /query endpoint
      logs    — /errors: captured warnings, failed jobs, failed relays
    """

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String)  # human label, e.g. 'claude-cli' | 'reporting-app'
    key_prefix: Mapped[str] = mapped_column(String, index=True)  # display only, e.g. 'owen_sk_7Fq3'
    key_hash: Mapped[str] = mapped_column(String, unique=True, index=True)  # sha256 hex of the plaintext
    scopes: Mapped[list] = mapped_column(JSONB, default=list)  # ['read', 'logs', ...]

    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_true())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiKeyUsage(Base):
    """One AI-API request. This is the audit trail for keys you hand to someone else.

    Written best-effort AFTER the response is produced — a failure to log must never fail a
    request. `sql` is populated only for /query, which is the whole point: you can see exactly
    what an external integration asked the database, not just that it asked something.

    Trimmed by the worker's retention sweep (API_USAGE_RETENTION_DAYS).
    """

    __tablename__ = "api_key_usage"
    __table_args__ = (Index("ix_api_key_usage_key_at", "api_key_id", "at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"))
    endpoint: Mapped[str] = mapped_column(String)  # route path, e.g. '/api/ai/calls/stats'
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    rows: Mapped[int | None] = mapped_column(Integer)  # rows returned, when meaningful
    sql: Mapped[str | None] = mapped_column(Text)  # /query only — the statement that ran
    error: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class AppLog(Base):
    """WARNING+ log records captured from BOTH the app and the worker container.

    Docker's json-file logs are the real log stream, but they live on the VPS behind SSH and
    the app container cannot read the worker's. Mirroring the serious records into Postgres is
    what makes `/api/ai/errors` possible at all — and it survives container restarts, which
    rotated json-file logs do not.

    Deliberately NOT the whole log stream: capture starts at WARNING (APP_LOG_CAPTURE_LEVEL)
    so this stays small on a box running native Postgres. Trimmed by the retention sweep.
    """

    __tablename__ = "app_logs"
    __table_args__ = (Index("ix_app_logs_at_level", "at", "level"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    service: Mapped[str] = mapped_column(String, index=True)  # 'app' | 'worker'
    level: Mapped[str] = mapped_column(String, index=True)  # WARNING | ERROR | CRITICAL
    logger: Mapped[str | None] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    # Call correlation when the record came from a `call.*` line — lets /errors answer
    # "what went wrong on this call" without parsing free text.
    linkedid: Mapped[str | None] = mapped_column(String, index=True)
    traceback: Mapped[str | None] = mapped_column(Text)

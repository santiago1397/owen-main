import uuid
from datetime import datetime

from pydantic import BaseModel


class CallListItem(BaseModel):
    id: uuid.UUID
    provider: str | None = None
    provider_call_sid: str
    direction: str | None = None
    status: str | None = None
    started_at: datetime | None = None
    duration_seconds: int | None = None
    caller_number: str | None = None
    dialed_number: str | None = None
    dialed_number_label: str | None = None  # Number.friendly_name of the tracking number
    campaign_name: str | None = None
    is_new_for_campaign: bool | None = None
    has_recording: bool = False
    category: str | None = None   # effective (override wins)
    is_spam: bool | None = None   # effective (override wins)


class Page(BaseModel):
    items: list
    page: int
    page_size: int
    total: int


class CallEventOut(BaseModel):
    event_type: str
    received_at: datetime


class RecordingOut(BaseModel):
    id: uuid.UUID
    status: str | None = None
    duration_seconds: int | None = None
    available: bool = False  # downloaded & playable


class AnalysisOut(BaseModel):
    is_spam: bool | None = None
    spam_confidence: float | None = None
    category: str | None = None
    tags: list[str] = []
    summary: str | None = None
    model: str | None = None
    category_override: str | None = None
    is_spam_override: bool | None = None


class TranscriptSegment(BaseModel):
    speaker: str  # 'caller' | 'operator'
    start: float | None = None
    end: float | None = None
    text: str


class CallDetail(CallListItem):
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    forwarded_to: str | None = None
    events: list[CallEventOut] = []
    recordings: list[RecordingOut] = []
    transcript: str | None = None  # flat text; fallback for mono/old/Twilio transcripts
    transcript_segments: list[TranscriptSegment] | None = None  # speaker-separated (stereo)
    analysis: AnalysisOut | None = None


class CallerUpdate(BaseModel):
    label: str | None = None


class AnalysisOverride(BaseModel):
    category_override: str | None = None
    is_spam_override: bool | None = None


class NumberStats(BaseModel):
    id: uuid.UUID
    phone_number: str
    friendly_name: str | None = None
    provider: str | None = None
    campaign_name: str | None = None
    forwards_to: str | None = None
    active: bool
    total_calls: int
    last_call_at: datetime | None = None
    # BulkVS + Asterisk platform (Ticket 03, additive; NULL for legacy Twilio/SignalWire).
    owner_provider: str | None = None   # who owns the DID, e.g. "bulkvs"
    media_provider: str | None = None   # who carries the media, e.g. "asterisk"
    released_at: datetime | None = None  # set when a synced DID vanished from BulkVS
    provider_status: str | None = None  # carrier-reported /tnRecord Status ("Active"/"SUBMITTED"/…)
    lifecycle: str = "available"        # DERIVED: available | pending | assigned | released


class CampaignOut(BaseModel):
    id: uuid.UUID
    name: str
    active: bool


class CallerOut(BaseModel):
    id: uuid.UUID
    phone_number: str
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    total_calls: int
    spam_score: float | None = None
    label: str | None = None


class FlowCreate(BaseModel):
    name: str


class FlowVersionSave(BaseModel):
    graph: dict  # nodes + per-node `next` port map (see app/flows/validator.py)


class FlowVersionOut(BaseModel):
    id: uuid.UUID
    flow_id: uuid.UUID
    version: int
    graph: dict
    created_at: datetime | None = None


class FlowOut(BaseModel):
    id: uuid.UUID
    name: str
    active_version_id: uuid.UUID | None = None
    created_at: datetime | None = None


class FlowDetail(FlowOut):
    versions: list[FlowVersionOut] = []


class ActivationResult(BaseModel):
    """Returned on successful activation. Hard-error activations are refused with HTTP 400
    whose detail carries the same {errors, warnings} shape."""

    activated: bool
    version_id: uuid.UUID
    errors: list[str] = []
    warnings: list[str] = []


# --- AI voice agents (Ticket 11) — mirrors the flow versioned-object schemas above ---

class AgentCreate(BaseModel):
    name: str


class AgentVersionSave(BaseModel):
    # persona/voice/greeting/model/engine/tools{name:bool}/knowledge/guardrails + extras.
    config: dict


class AgentVersionOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    version: int
    config: dict
    created_at: datetime | None = None


class AgentOut(BaseModel):
    id: uuid.UUID
    name: str
    active_version_id: uuid.UUID | None = None
    created_at: datetime | None = None


class AgentDetail(AgentOut):
    versions: list[AgentVersionOut] = []


class AgentActivationResult(BaseModel):
    activated: bool
    version_id: uuid.UUID
    errors: list[str] = []
    warnings: list[str] = []


class DashboardSummary(BaseModel):
    range_from: datetime
    range_to: datetime
    total_calls: int
    spam_calls: int = 0
    junk_calls: int = 0  # likely-junk heuristic (short/never-connected), counted over the range
    avg_duration_seconds: float | None = None
    new_callers_global: int
    returning_callers_global: int
    new_for_campaign: int
    returning_for_campaign: int
    by_campaign: list[dict]
    by_number: list[dict]
    daily: list[dict]
    by_hour: list[dict]  # 24 rows, hour 0–23 in business tz, zero-filled
    top_callers: list[dict]

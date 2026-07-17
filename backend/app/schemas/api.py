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


class CallDetail(CallListItem):
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    forwarded_to: str | None = None
    events: list[CallEventOut] = []
    recordings: list[RecordingOut] = []
    transcript: str | None = None
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


class CallerOut(BaseModel):
    id: uuid.UUID
    phone_number: str
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    total_calls: int
    spam_score: float | None = None
    label: str | None = None


class DashboardSummary(BaseModel):
    range_from: datetime
    range_to: datetime
    total_calls: int
    spam_calls: int = 0
    avg_duration_seconds: float | None = None
    new_callers_global: int
    returning_callers_global: int
    new_for_campaign: int
    returning_for_campaign: int
    by_campaign: list[dict]
    by_number: list[dict]
    daily: list[dict]
    top_callers: list[dict]

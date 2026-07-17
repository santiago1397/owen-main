"""Provider-agnostic interface. Twilio and SignalWire each implement this so the
rest of the app never branches on provider (see ARCHITECTURE.md #12).

Only `verify_signature` and `download_recording` are genuinely per-provider; the
payload parsing is nearly identical because SignalWire mirrors Twilio's cXML fields.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


# Status rank guards out-of-order webhook arrival (see Call.status_rank).
STATUS_RANK = {
    "initiated": 1,
    "ringing": 2,
    "in-progress": 3,
    "answered": 3,
    "completed": 4,
    "busy": 4,
    "no-answer": 4,
    "failed": 4,
    "canceled": 4,
}


@dataclass
class NormalizedCallEvent:
    provider_call_sid: str
    event_type: str
    status: str | None = None
    from_number: str | None = None
    to_number: str | None = None
    direction: str | None = None
    started_at: datetime | None = None
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: int | None = None
    forwarded_to: str | None = None
    provider_sequence: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def status_rank(self) -> int:
        return STATUS_RANK.get((self.status or "").lower(), 0)


@dataclass
class NormalizedRecordingEvent:
    provider_call_sid: str
    provider_recording_sid: str
    status: str | None
    duration_seconds: int | None
    provider_url: str | None
    raw: dict = field(default_factory=dict)


@dataclass
class NormalizedMessageEvent:
    provider_message_sid: str
    from_number: str | None = None
    to_number: str | None = None
    body: str | None = None
    status: str | None = None
    num_media: int = 0
    media_urls: list[str] = field(default_factory=list)
    direction: str = "inbound"
    raw: dict = field(default_factory=dict)


class ProviderAdapter(Protocol):
    name: str

    def parse_status_event(self, params: dict[str, str]) -> NormalizedCallEvent: ...
    def parse_recording_event(self, params: dict[str, str]) -> NormalizedRecordingEvent: ...
    def parse_message_event(self, params: dict[str, str]) -> NormalizedMessageEvent: ...
    def verify_signature(self, url: str, params: dict[str, str], signature: str) -> bool: ...

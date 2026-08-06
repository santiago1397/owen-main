"""Provider-agnostic interface. Twilio and SignalWire each implement this so the
rest of the app never branches on provider (see ARCHITECTURE.md #12).

Only `verify_signature` and `download_recording` are genuinely per-provider; the
payload parsing is nearly identical because SignalWire mirrors Twilio's cXML fields.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


# Fewest digits a real tracking DID can have (a NANP number is 10-11; short codes are 5-6).
_MIN_DID_DIGITS = 5


def looks_like_tracking_number(value: str | None) -> bool:
    """Could `value` plausibly be a real DID someone forgot to register?

    Not every "to" value reaching ingestion is a phone number. Asterisk's dialplan routes
    through PSEUDO-EXTENSIONS — `s` (start), `h` (hangup), `i` (invalid), `t` (timeout) — and
    those arrive as the tracking number on secondary channel events for calls that are already
    correctly attributed from their entry event. Warning about them produced 242 "no registered
    Number" lines in 24h on a day with 25 calls, which is worse than useless: the real signal
    (a genuine DID pointed at OWEN that nobody registered, so its calls lose campaign
    attribution) was buried in noise nobody could read.

    So the split is by SHAPE, not by a hardcoded list of Asterisk's extension names — a
    provider-agnostic rule that stays true for the next provider: enough digits to be a number
    at all -> a real attribution gap, warn; anything else -> a dialplan artifact, debug.

    Lives here, in the stdlib-only provider base, rather than next to its caller in
    services/ingestion.py, so it is unit-testable without sqlalchemy.
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return len(digits) >= _MIN_DID_DIGITS


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

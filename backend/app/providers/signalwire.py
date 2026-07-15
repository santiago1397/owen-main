"""SignalWire adapter.

SignalWire's Compatibility API mirrors Twilio's cXML field names, so parsing is shared
with the Twilio adapter. Only signature verification is genuinely provider-specific
(ARCHITECTURE.md #12), so that's the sole override.
"""

from app.core.security import verify_signalwire_signature
from app.providers.base import NormalizedCallEvent, NormalizedRecordingEvent
from app.providers.twilio import TwilioAdapter


class SignalWireAdapter(TwilioAdapter):
    name = "signalwire"

    def parse_status_event(self, params: dict[str, str]) -> NormalizedCallEvent:
        return super().parse_status_event(params)

    def parse_recording_event(self, params: dict[str, str]) -> NormalizedRecordingEvent:
        return super().parse_recording_event(params)

    def verify_signature(self, url: str, params: dict[str, str], signature: str) -> bool:
        return verify_signalwire_signature(url, params, signature)

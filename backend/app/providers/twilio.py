from datetime import datetime, timezone

from app.core.security import verify_twilio_signature
from app.providers.base import (
    NormalizedCallEvent,
    NormalizedMessageEvent,
    NormalizedRecordingEvent,
    ProviderAdapter,
)


def _to_int(v: str | None) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


class TwilioAdapter(ProviderAdapter):
    name = "twilio"

    def parse_status_event(self, params: dict[str, str]) -> NormalizedCallEvent:
        status = params.get("CallStatus")
        return NormalizedCallEvent(
            provider_call_sid=params.get("CallSid", ""),
            event_type=status or "status",
            status=status,
            from_number=params.get("From"),
            to_number=params.get("To"),
            direction=params.get("Direction"),
            duration_seconds=_to_int(params.get("CallDuration")),
            forwarded_to=params.get("ForwardedFrom"),
            # Twilio has no monotonic sequence; use Sid+status as the dedup key.
            provider_sequence=f"{params.get('CallSid', '')}:{status}",
            raw=dict(params),
        )

    def parse_recording_event(self, params: dict[str, str]) -> NormalizedRecordingEvent:
        return NormalizedRecordingEvent(
            provider_call_sid=params.get("CallSid", ""),
            provider_recording_sid=params.get("RecordingSid", ""),
            status=params.get("RecordingStatus"),
            duration_seconds=_to_int(params.get("RecordingDuration")),
            provider_url=params.get("RecordingUrl"),
            raw=dict(params),
        )

    def parse_message_event(self, params: dict[str, str]) -> NormalizedMessageEvent:
        # cXML/Compatibility inbound-SMS fields (SignalWire mirrors these). Collect any
        # MMS media (MediaUrl0..N, count in NumMedia).
        num_media = _to_int(params.get("NumMedia")) or 0
        media_urls = [
            url for i in range(num_media)
            if (url := params.get(f"MediaUrl{i}"))
        ]
        return NormalizedMessageEvent(
            provider_message_sid=params.get("MessageSid") or params.get("SmsSid", ""),
            from_number=params.get("From"),
            # Trust the tracking-number query-string override we control (webhooks/common.py)
            # over the payload's To, mirroring the status-event handling.
            to_number=params.get("_tracking_number") or params.get("To"),
            body=params.get("Body"),
            status=params.get("MessageStatus") or params.get("SmsStatus"),
            num_media=num_media,
            media_urls=media_urls,
            direction="inbound",
            raw=dict(params),
        )

    def verify_signature(self, url: str, params: dict[str, str], signature: str) -> bool:
        return verify_twilio_signature(url, params, signature)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

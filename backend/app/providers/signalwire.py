"""SignalWire adapter.

SignalWire's Compatibility API mirrors Twilio's cXML field names, so classic webhook
parsing is shared with the Twilio adapter. But Call Flow Builder's "Call State URL"
field doesn't send that shape at all — it sends SignalWire's own native Calling/Relay
event schema (event_type: "calling.call.state", with a stringified `params` blob).
Detect and parse that shape separately; fall back to the Twilio-compatible parser for
anything else (e.g. a future classic LaML webhook config).
"""

import ast
from datetime import datetime, timezone

from app.core.security import verify_signalwire_signature
from app.providers.base import NormalizedCallEvent, NormalizedRecordingEvent
from app.providers.twilio import TwilioAdapter

_CALL_STATE_TO_STATUS = {
    "created": "initiated",
    "ringing": "ringing",
    "answered": "in-progress",
    "ended": "completed",
}

_END_REASON_TO_STATUS = {
    "busy": "busy",
    "no_answer": "no-answer",
    "timeout": "no-answer",
    "cancel": "canceled",
    "canceled": "canceled",
    "error": "failed",
}


def _parse_native_params(raw) -> dict:
    """The `params` field arrives as a Python-repr string (single-quoted), not JSON."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return {}
    return {}


def _ms_to_dt(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


class SignalWireAdapter(TwilioAdapter):
    name = "signalwire"

    def parse_status_event(self, params: dict[str, str]) -> NormalizedCallEvent:
        if params.get("event_type") != "calling.call.state":
            return super().parse_status_event(params)

        p = _parse_native_params(params.get("params"))
        call_state = (p.get("call_state") or "").lower()
        status = _CALL_STATE_TO_STATUS.get(call_state, call_state or None)
        if call_state == "ended":
            status = _END_REASON_TO_STATUS.get((p.get("end_reason") or "").lower(), "completed")

        # A Forward/Connect leg's own event nests the original inbound call under
        # `parent` — correlate to that, not this leg's own (throwaway) call_id, so the
        # forwarded leg's state updates land on the same Call row the inbound leg created.
        parent = p.get("parent") or {}
        call_sid = parent.get("call_id") or p.get("call_id") or ""

        device_params = (p.get("device") or {}).get("params") or {}
        from_number = device_params.get("from_number")
        # The payload's own to_number is the forward target, not the tracking number —
        # trust the query-string override we control (see webhooks/common.py) instead.
        to_number = params.get("_tracking_number") or device_params.get("to_number")

        return NormalizedCallEvent(
            provider_call_sid=call_sid,
            event_type=call_state or "status",
            status=status,
            from_number=from_number,
            to_number=to_number,
            direction="inbound",  # Call Flow Builder flows here are inbound-triggered only.
            started_at=_ms_to_dt(p.get("start_time")),
            answered_at=_ms_to_dt(p.get("answer_time")),
            ended_at=_ms_to_dt(p.get("end_time")),
            duration_seconds=(
                int((p["end_time"] - p["start_time"]) / 1000)
                if p.get("end_time") and p.get("start_time") else None
            ),
            provider_sequence=f"{call_sid}:{call_state}",
            raw=dict(params) | {"_parsed_params": p},
        )

    def parse_recording_event(self, params: dict[str, str]) -> NormalizedRecordingEvent:
        return super().parse_recording_event(params)

    # parse_message_event is inherited from TwilioAdapter: the number's Inbound Message
    # resource is a cXML/Compatibility Messaging webhook, whose fields (MessageSid, From,
    # To, Body, NumMedia, MediaUrl*) match Twilio's. Override only if a future native
    # (Relay) message shape gets used here.

    def verify_signature(self, url: str, params: dict[str, str], signature: str) -> bool:
        return verify_signalwire_signature(url, params, signature)

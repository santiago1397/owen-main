"""Asterisk (BulkVS) provider adapter — ARI event schema, NOT a signed webhook.

Unlike Twilio/SignalWire, Asterisk events don't arrive as a signed public HTTP POST;
they stream over an authenticated localhost ARI WebSocket (see workers/asterisk_consumer.py).
So `verify_signature` is a deliberate NO-OP: the transport is the trust boundary.

This adapter's job is to normalize a raw ARI *channel* event dict into the SAME
`NormalizedCallEvent` the projection already understands, projecting Asterisk's channel
lifecycle onto the existing Twilio-CallStatus vocabulary (STATUS_RANK in base.py) so an
Asterisk call flows through `ingest_status_event` identically to a Twilio/SignalWire one.

Two locked design decisions live here:
- `provider_call_sid = Linkedid` → exactly one `calls` row per call; every leg of the
  call (inbound + any outbound/agent legs) shares the Linkedid and collapses onto it.
- Status is ranked off the ENTRY channel only (the inbound leg, whose channel Uniqueid
  == the call's Linkedid). Secondary legs are ignored for status projection, which is
  what stops a forwarded/dialed leg from double-counting the call (the same hazard the
  reconciler's Twilio-only `_is_inbound` guards against — but we solve it structurally
  here rather than reusing that Twilio-only rule).

Requires `channelvars=Linkedid` in ari.conf so every channel event carries
`channel.channelvars.Linkedid`; without it we cannot tell the entry leg from a secondary
leg. (Asterisk sets a new channel's Linkedid to the Uniqueid of the first channel in the
call, so the entry leg's own id equals the Linkedid.)
"""

from datetime import datetime, timezone

from app.providers.base import (
    STATUS_RANK,
    NormalizedCallEvent,
    NormalizedMessageEvent,
    NormalizedRecordingEvent,
    ProviderAdapter,
)

# ARI channel-lifecycle event type -> Twilio-CallStatus vocabulary. Events not listed
# here (or ChannelStateChange states not in _STATE_TO_STATUS, or ChannelDestroyed causes
# handled below) yield status=None and are skipped by the consumer.
#   StasisStart     : entry channel handed to our Stasis app  -> "initiated" (rank 1)
#   ChannelDestroyed: channel torn down                       -> terminal, per Q.850 cause
#   StasisEnd       : channel LEFT the app (may still be live) -> not terminal on its own
_ARI_TO_STATUS = {
    "StasisStart": "initiated",
    "ChannelDestroyed": "__cause__",  # resolved from the Q.850 cause code below
    "StasisEnd": None,
}

# ChannelStateChange.channel.state -> status. Asterisk channel states, lower-cased.
_STATE_TO_STATUS = {
    "ring": "ringing",      # outbound-ish "we are ringing them"
    "ringing": "ringing",   # inbound "the far end is ringing us"
    "up": "in-progress",    # answered / media flowing
}

# ChannelDestroyed Q.850 hangup cause -> terminal status. Unlisted causes -> "failed".
_CAUSE_TO_STATUS = {
    16: "completed",   # normal clearing
    17: "busy",        # user busy
    18: "no-answer",   # no user responding
    19: "no-answer",   # no answer from user (user alerted, no answer)
    21: "no-answer",   # call rejected
}

# Bound the in-memory dedup set so a very long-lived worker can't grow it without limit.
_DEDUP_MAX = 50_000


def _channel(event: dict) -> dict:
    ch = event.get("channel")
    return ch if isinstance(ch, dict) else {}


def linkedid(event: dict) -> str:
    """The call's Linkedid = the provider_call_sid all legs collapse under.

    Read from `channel.channelvars.Linkedid` (needs `channelvars=Linkedid` in ari.conf).
    Falls back to the channel's own id, which is correct for the entry leg."""
    ch = _channel(event)
    cv = ch.get("channelvars")
    if isinstance(cv, dict) and cv.get("Linkedid"):
        return str(cv["Linkedid"])
    return str(ch.get("id") or "")


def is_entry_channel(event: dict) -> bool:
    """True iff this event's channel is the inbound entry leg (Uniqueid == Linkedid).
    Status is ranked off the entry channel only, so secondary legs never double-count."""
    ch = _channel(event)
    cid = str(ch.get("id") or "")
    return bool(cid) and cid == linkedid(event)


def _status_for(event: dict) -> str | None:
    etype = event.get("type")
    mapped = _ARI_TO_STATUS.get(etype)
    if etype == "ChannelStateChange":
        state = str(_channel(event).get("state") or "").lower()
        return _STATE_TO_STATUS.get(state)
    if mapped == "__cause__":  # ChannelDestroyed
        cause = event.get("cause")
        try:
            cause = int(cause)
        except (TypeError, ValueError):
            cause = None
        return _CAUSE_TO_STATUS.get(cause, "failed")
    return mapped


def _parse_ts(event: dict) -> datetime | None:
    """ARI stamps every event with an ISO-8601 `timestamp` (offset-aware)."""
    raw = event.get("timestamp")
    if not raw:
        return None
    try:
        # Asterisk uses e.g. "2026-07-22T14:03:11.123+0000"; normalize the tz colon.
        s = str(raw)
        if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
            s = s[:-2] + ":" + s[-2:]
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


class AsteriskAdapter(ProviderAdapter):
    name = "asterisk"

    def parse_status_event(self, params: dict) -> NormalizedCallEvent:
        """Normalize a raw ARI channel event dict. `status` is None for events that
        don't map onto the status vocabulary (the consumer skips those)."""
        ch = _channel(params)
        status = _status_for(params)
        lid = linkedid(params)
        ts = _parse_ts(params)

        # DID dialed = the tracking number; caller = the PSTN number that called in.
        exten = (ch.get("dialplan") or {}).get("exten")
        caller_number = (ch.get("caller") or {}).get("number")

        rank = STATUS_RANK.get(status.lower(), 0) if status else 0

        return NormalizedCallEvent(
            provider_call_sid=lid,
            event_type=params.get("type") or "asterisk",
            status=status,
            from_number=caller_number,
            to_number=exten,
            # Inbound PSTN entry leg. Outbound/agent calls become the same rows later,
            # distinguished by direction; the reconciler's `_is_inbound` drop stays
            # Twilio-only and is never applied to asterisk.
            direction="inbound",
            started_at=ts if rank == 1 else None,
            answered_at=ts if status == "in-progress" else None,
            ended_at=ts if rank >= 4 else None,
            # Dedup key: one row per (Linkedid, status). Mirrors Twilio's Sid:status key.
            provider_sequence=f"{lid}:{status}" if status else None,
            raw=dict(params),
        )

    # Recordings + CDR reconciliation are a separate downstream ticket (05). Asterisk is
    # not wired to the webhook router, so these are never called on the ingestion path;
    # present only to satisfy the ProviderAdapter shape.
    def parse_recording_event(self, params: dict) -> NormalizedRecordingEvent:
        raise NotImplementedError("asterisk recording ingestion is ticket 05")

    def parse_message_event(self, params: dict) -> NormalizedMessageEvent:
        raise NotImplementedError("asterisk SMS ingestion is a later ticket")

    def verify_signature(self, url: str, params: dict, signature: str) -> bool:
        # NO-OP: ARI events arrive over an authenticated localhost WebSocket, not a
        # signed public webhook. The transport (loopback + ARI creds) is the trust
        # boundary; there is no per-event signature to verify.
        return True


class AsteriskEventRouter:
    """Pure, synchronous router the WS consumer feeds every ARI event through.

    `route(event)` returns a NormalizedCallEvent ready for `ingest_status_event`, or None
    to skip. It is dependency-free (no WS, no DB) precisely so the mapping + entry-channel
    ranking + dedup rules are unit-testable in isolation. Skips when:
      - the event doesn't map onto the status vocabulary (status is None), or
      - it isn't the inbound entry channel (status is ranked off the entry leg only), or
      - `"{Linkedid}:{status}"` has already been seen (dedup suppresses leg/retry noise).
    """

    def __init__(self, adapter: "AsteriskAdapter | None" = None) -> None:
        self.adapter = adapter or AsteriskAdapter()
        self._seen: set[str] = set()

    def route(self, event: dict) -> NormalizedCallEvent | None:
        evt = self.adapter.parse_status_event(event)
        if not evt.provider_call_sid or evt.status is None:
            return None
        if not is_entry_channel(event):
            return None
        key = evt.provider_sequence or f"{evt.provider_call_sid}:{evt.status}"
        if key in self._seen:
            return None
        if len(self._seen) >= _DEDUP_MAX:
            self._seen.clear()  # coarse but safe; dedup is a best-effort noise filter
        self._seen.add(key)
        return evt

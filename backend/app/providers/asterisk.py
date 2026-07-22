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

# ARI recording-lifecycle event types the consumer routes to `parse_recording_event`.
# We only act on the *finished* signal (the WAV is fully written on the host by then);
# RecordingStarted/RecordingFailed carry no completed audio to hand to the pipeline.
RECORDING_EVENT_TYPES = frozenset({"RecordingFinished"})

# ARI StoredRecording.state -> recordings-table status string. `done` is the success
# terminal; anything else is stored as-is so the row reflects what Asterisk reported.
# Mirrors the "completed" label a Twilio recording lands with.
_REC_STATE_TO_STATUS = {
    "done": "completed",
    "failed": "failed",
    "canceled": "canceled",
}

# Asterisk CDR `disposition` -> Twilio-CallStatus vocabulary (same ranks as _CAUSE_TO_STATUS
# above). The CDR reconciler projects a missed call's terminal status from this. Unlisted /
# blank dispositions fall back to "failed" (a safe rank-4 terminal). Values are upper-cased
# before lookup because cdr_pgsql stores them upper-case ("ANSWERED", "NO ANSWER", ...).
_DISPOSITION_TO_STATUS = {
    "ANSWERED": "completed",
    "NO ANSWER": "no-answer",
    "NOANSWER": "no-answer",
    "BUSY": "busy",
    "FAILED": "failed",
    "CONGESTION": "failed",
}


def recording_linkedid(name: str) -> str:
    """The call's Linkedid the interpreter prefixed onto a recording name.

    The flow interpreter names every recording `{linkedid}-{tag}-{counter}` (see
    interpreter._rec_name). Asterisk uniqueids are `<epoch>.<seq>` — no dashes — so the
    Linkedid is unambiguously the substring before the first '-'."""
    return str(name or "").split("-", 1)[0]


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

    def parse_recording_event(self, params: dict) -> NormalizedRecordingEvent:
        """Normalize an ARI `RecordingFinished` event into the SAME NormalizedRecordingEvent
        the existing pipeline consumes (services/recordings.ingest_recording_event).

        The recording's `name` is our idempotency key (`provider_recording_sid`), and its
        `{linkedid}-...` prefix is the `provider_call_sid` every leg collapses under — so the
        recording attaches to the exact `calls` row the status projection built. There is no
        HTTP `provider_url`: Asterisk wrote a WAV to a local spool dir (a bind-mount makes it
        visible to the app container), so the "fetch" is a local file move, not a download —
        `provider_url` is None and the fetch handler routes on provider name instead."""
        rec = params.get("recording")
        rec = rec if isinstance(rec, dict) else {}
        name = str(rec.get("name") or "")
        state = str(rec.get("state") or "").lower()
        duration = rec.get("duration")
        try:
            duration = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        return NormalizedRecordingEvent(
            provider_call_sid=recording_linkedid(name),
            provider_recording_sid=name,
            status=_REC_STATE_TO_STATUS.get(state, state or None),
            duration_seconds=duration,
            provider_url=None,  # local spool file; fetch is a move, not an HTTP GET
            raw=dict(params),
        )

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

    def route_recording(self, event: dict) -> NormalizedRecordingEvent | None:
        """Return a NormalizedRecordingEvent for a `RecordingFinished` event, else None.

        Kept beside `route` (and equally dependency-free) so the consumer has one pure entry
        point per event and recording normalization is unit-testable without a WS or DB.
        Skips events that aren't recording-finished, or that carry no name/linkedid."""
        if event.get("type") not in RECORDING_EVENT_TYPES:
            return None
        rec = self.adapter.parse_recording_event(event)
        if not rec.provider_recording_sid or not rec.provider_call_sid:
            return None
        return rec


def cdr_row_to_event(row: dict) -> NormalizedCallEvent | None:
    """Project one Asterisk CDR row (from cdr_pgsql, read as a dict) into the SAME
    NormalizedCallEvent the WS consumer produces, so a CDR-reconciled call is
    indistinguishable in the projection from a live-ingested one.

    Pure + dependency-free (no DB) so it is unit-testable in isolation.

    Two invariants make the CDR reconcile idempotent AND non-double-counting against the
    live WS path:
      - ENTRY-LEG ONLY: keep the row whose `uniqueid == linkedid` (the inbound entry leg),
        exactly the structural rule `is_entry_channel` applies to WS events. Secondary
        (dialed/agent) legs share the linkedid and are dropped, so a forwarded call is one
        `calls` row, never two.
      - SAME DEDUP KEY: `provider_sequence = "{linkedid}:{status}"` — byte-identical to the
        WS adapter's key — so a CDR terminal event and a WS terminal event of the same
        status collapse onto ONE `call_events` row (call_events' natural key dedup), and
        re-running the reconciler never inserts a duplicate.
    Returns None to skip (no linkedid, or not the entry leg)."""
    linkedid = str(row.get("linkedid") or "")
    uniqueid = str(row.get("uniqueid") or "")
    if not linkedid:
        return None
    # Entry-leg only. If uniqueid is absent (some CDR configs omit it) we can't prove this is
    # a secondary leg, so we keep it rather than silently dropping a real call.
    if uniqueid and uniqueid != linkedid:
        return None

    disposition = str(row.get("disposition") or "").strip().upper()
    status = _DISPOSITION_TO_STATUS.get(disposition, "failed")

    def _int(v):
        try:
            return int(v) if v is not None and str(v) != "" else None
        except (TypeError, ValueError):
            return None

    def _dt(v):
        if not v:
            return None
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(str(v))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return None

    return NormalizedCallEvent(
        provider_call_sid=linkedid,
        event_type="cdr",
        status=status,
        from_number=(row.get("src") or None),
        to_number=(row.get("dst") or None),
        direction="inbound",
        started_at=_dt(row.get("start")),
        answered_at=_dt(row.get("answer")),
        ended_at=_dt(row.get("end")),
        # billsec is the answered-duration; fall back to total duration.
        duration_seconds=_int(row.get("billsec")) or _int(row.get("duration")),
        provider_sequence=f"{linkedid}:{status}",
        raw=dict(row),
    )

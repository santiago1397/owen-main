"""OWEN call lifecycle -> the CRM's `POST /api/events` body. PURE (stdlib only).

## The contract, READ FROM THE CRM'S SOURCE, not guessed

`ghl-clone` (`backend/app/main.py::EventIngest` / `ingest_event`) accepts exactly:

    {"contact_id": int,                                  # REQUIRED, must already exist
     "type": "SMS" | "CALL" | "EMAIL" | "INTERNAL_COMMENT",
     "direction": "INBOUND" | "OUTBOUND",                # default "INBOUND"
     "body": str | None,
     "duration_seconds": int | None,
     "call_status": str | None,                          # CALL only
     "recording_url": str | None,
     "provider_ref": str | None}

and enforces three rules we must satisfy or be rejected with a 400/404:

  1. `contact_id` must resolve to an existing Contact, else **404**. There is no
     create-on-ingest and no lookup by phone number on this endpoint, so a caller who is
     not already in the CRM has nowhere to land. See `client.resolve_contact_id`.
  2. `call_status` is accepted ONLY when `type == "CALL"`, and only from
     `{completed, no-answer, busy, voicemail, failed}` — anything else is a 400.
  3. Authorisation is `Bearer ghl_pat_...` whose token carries the `events:write` scope
     AND whose owning user is ADMIN or DISPATCHER (`auth.require_events_ingest`).

## Why a call produces the events it does

`ingest_event` runs `automations.on_inbound_call`, which fires the **missed-call auto
text-back** rule for any INBOUND CALL whose `duration_seconds` is <= 15 (or absent). So a
"call started" row sent as `type: CALL` with no duration would tell the CRM every single
call was missed, and queue a text to the customer while they are still on the phone.

Therefore:

  * `started` and `answered` map to **INTERNAL_COMMENT**, direction OUTBOUND. The CRM
    records those as internal thread entries (`automations.INTERNAL_TYPES`) — never
    transmitted, no automation, and OUTBOUND so they do not inflate the thread's unread
    badge for something no human sent.
  * `ended` maps to **CALL** with the real duration and a real `call_status`, so the
    missed-call rule fires exactly once per call, on the truth.

(In this CRM's v1 the outbound transport is `LoggingTransport` — the text-back is recorded
as intent and nothing is transmitted. That is the CRM's decision to reverse, not ours, so
this module is built as though the text were real.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# The three lifecycle phases this module reports.
PHASE_STARTED = "started"
PHASE_ANSWERED = "answered"
PHASE_ENDED = "ended"
PHASES = (PHASE_STARTED, PHASE_ANSWERED, PHASE_ENDED)

# Verified against ghl-clone `main.py::CALL_STATUSES`. Sending anything else is a 400.
CRM_CALL_STATUSES = frozenset({"completed", "no-answer", "busy", "voicemail", "failed"})

# Verified against ghl-clone `models.py::EventType` / `EventIngest.type`.
CRM_TYPE_CALL = "CALL"
CRM_TYPE_INTERNAL = "INTERNAL_COMMENT"

# Below this the CRM treats an inbound call as MISSED and queues the text-back
# (`automations.MISSED_CALL_MAX_SECONDS`). Mirrored here only so the summary line we write
# does not contradict the row the CRM derives from it.
CRM_MISSED_CALL_MAX_SECONDS = 15

# How an OWEN ring outcome becomes one of the five statuses the CRM will accept.
# `answered` is the hybrid ring group's bridged outcome; `noanswer`/`failed` are its other
# two ports; `voicemail` is set by the handler when the caller actually left one.
_OUTCOME_TO_CRM_STATUS = {
    "answered": "completed",
    "voicemail": "voicemail",
    "noanswer": "no-answer",
    "busy": "busy",
    "failed": "failed",
}


def crm_call_status(outcome: str | None) -> str:
    """Map an OWEN ring outcome onto the CRM's five-value vocabulary.

    An unknown outcome becomes "failed" rather than "completed": reporting a call we cannot
    account for as completed would quietly inflate the CRM's answered-call report, and an
    over-reported failure is a question somebody asks, while an over-reported success is not.
    """
    return _OUTCOME_TO_CRM_STATUS.get(str(outcome or "").lower(), "failed")


@dataclass(frozen=True)
class CallEventFacts:
    """Everything OWEN knows about one lifecycle moment of one call.

    This is the module's OWN shape — the CRM-agnostic body carried in the `crm_report` job
    payload. Mapping it onto a specific CRM happens in `to_crm_event`, at the app-side
    adapter, so the queue and the worker never learn what a CRM is (the same split
    `CRM_CONTEXT_SPEC` C15/C16 settled for the agent's context provider).
    """

    phase: str                                  # started | answered | ended
    owen_call_id: str                           # calls.id — the CRM stores this as owen_call_id
    linkedid: str = ""                          # calls.provider_call_sid (Asterisk Linkedid)
    caller_number: str = ""
    dialed_number: str = ""
    direction: str = "inbound"
    outcome: str = ""                           # answered | voicemail | noanswer | busy | failed
    duration_seconds: Optional[int] = None
    winning_destination: Optional[str] = None   # which leg won: an operator id or an E.164
    winning_kind: Optional[str] = None          # "operator" | "pstn"
    recording_id: Optional[str] = None
    transcript_id: Optional[str] = None
    owen_url: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def as_payload(self) -> dict:
        """The job-payload body. Plain JSON, no None-stripping — an explicit null is a fact
        ("there is no recording"), and stripping it would make an old payload and a new one
        indistinguishable in the `jobs` table."""
        return {
            "phase": self.phase,
            "owen_call_id": self.owen_call_id,
            "linkedid": self.linkedid,
            "caller_number": self.caller_number,
            "dialed_number": self.dialed_number,
            "direction": self.direction,
            "outcome": self.outcome,
            "duration_seconds": self.duration_seconds,
            "winning_destination": self.winning_destination,
            "winning_kind": self.winning_kind,
            "recording_id": self.recording_id,
            "transcript_id": self.transcript_id,
            "owen_url": self.owen_url,
            "extra": dict(self.extra or {}),
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "CallEventFacts":
        p = dict(payload or {})
        raw_duration = p.get("duration_seconds")
        try:
            duration = None if raw_duration is None else int(raw_duration)
        except (TypeError, ValueError):
            duration = None
        return cls(
            phase=str(p.get("phase") or PHASE_ENDED),
            owen_call_id=str(p.get("owen_call_id") or ""),
            linkedid=str(p.get("linkedid") or ""),
            caller_number=str(p.get("caller_number") or ""),
            dialed_number=str(p.get("dialed_number") or ""),
            direction=str(p.get("direction") or "inbound"),
            outcome=str(p.get("outcome") or ""),
            duration_seconds=duration,
            winning_destination=p.get("winning_destination") or None,
            winning_kind=p.get("winning_kind") or None,
            recording_id=p.get("recording_id") or None,
            transcript_id=p.get("transcript_id") or None,
            owen_url=p.get("owen_url") or None,
            extra=p.get("extra") if isinstance(p.get("extra"), dict) else {},
        )


def summary_line(facts: CallEventFacts) -> str:
    """The human-readable line that lands on the CRM's conversation thread.

    Everything the owner asked to see is in here in words, because the CRM's event row has
    exactly four typed slots (duration, status, recording url, provider ref) and no field
    for "which of the three phones answered" — the fact that makes a hybrid ring group worth
    having. `owen_call_id` is repeated in the text as well as in `provider_ref` so the join
    key survives a copy-paste out of the UI.
    """
    bits: list[str] = []
    direction = "Inbound" if str(facts.direction).lower() != "outbound" else "Outbound"
    if facts.phase == PHASE_STARTED:
        bits.append(f"{direction} call started")
    elif facts.phase == PHASE_ANSWERED:
        bits.append(f"{direction} call answered")
    else:
        bits.append(f"{direction} call ended")

    if facts.caller_number:
        bits.append(f"from {facts.caller_number}")
    if facts.dialed_number:
        bits.append(f"to {facts.dialed_number}")

    line = " ".join(bits) + "."
    tail: list[str] = []
    if facts.winning_destination:
        kind = facts.winning_kind or "destination"
        tail.append(f"Answered by {kind} {facts.winning_destination}.")
    if facts.phase == PHASE_ENDED:
        if facts.outcome:
            tail.append(f"Outcome: {facts.outcome}.")
        if facts.duration_seconds is not None:
            tail.append(f"Duration: {facts.duration_seconds}s.")
    if facts.recording_id:
        tail.append(f"OWEN recording {facts.recording_id}.")
    if facts.transcript_id:
        tail.append(f"OWEN transcript {facts.transcript_id}.")
    if facts.owen_call_id:
        tail.append(f"owen_call_id={facts.owen_call_id}")
    if facts.owen_url:
        tail.append(str(facts.owen_url))
    return " ".join([line, *tail]).strip()


def to_crm_event(facts: CallEventFacts, contact_id: int) -> dict[str, Any]:
    """Build the exact body `POST /api/events` accepts. See the module docstring for why
    the two pre-terminal phases are INTERNAL_COMMENT rather than CALL.

    `provider_ref` carries `calls.id` — the value the CRM stores as
    `Opportunity.custom_fields.owen_call_id`, which is the documented join key back here.
    """
    direction = "OUTBOUND" if str(facts.direction).lower() == "outbound" else "INBOUND"
    body: dict[str, Any] = {
        "contact_id": int(contact_id),
        "body": summary_line(facts),
        # calls.id, on EVERY phase — the join key must not depend on which event survived.
        "provider_ref": facts.owen_call_id or facts.linkedid or None,
    }

    if facts.phase != PHASE_ENDED:
        # Internal note. Direction OUTBOUND on purpose: the CRM increments the thread's
        # unread badge for INBOUND rows, and a machine-written progress note is not
        # something a person needs to acknowledge.
        body["type"] = CRM_TYPE_INTERNAL
        body["direction"] = "OUTBOUND"
        return body

    body["type"] = CRM_TYPE_CALL
    body["direction"] = direction
    body["call_status"] = crm_call_status(facts.outcome)
    if facts.duration_seconds is not None:
        body["duration_seconds"] = max(0, int(facts.duration_seconds))
    # The CRM's column is a URL, and OWEN's recordings are served behind a short-lived
    # signed playback token that would be expired by the time anyone clicked it. Sending the
    # RECORDING ID in the body text (above) and leaving this null is honest; a permanent
    # unauthenticated media URL is a decision for the owner, not a side effect of this build.
    body["recording_url"] = None
    return body


def validate_crm_event(body: dict) -> list[str]:
    """Everything the CRM would reject, checked here first. Returns a list of problems
    (empty = the CRM's own validators will accept this shape).

    This exists so a contract drift shows up in `make check` as a failing unit test rather
    than as a 400 in a retry loop against a live CRM.
    """
    problems: list[str] = []
    if not isinstance(body.get("contact_id"), int):
        problems.append("contact_id must be an int")
    etype = body.get("type")
    if etype not in ("SMS", "CALL", "EMAIL", "INTERNAL_COMMENT"):
        problems.append(f"type {etype!r} is not one of the CRM's four event types")
    if body.get("direction") not in ("INBOUND", "OUTBOUND"):
        problems.append("direction must be INBOUND or OUTBOUND")
    status = body.get("call_status")
    if status is not None:
        if etype != "CALL":
            problems.append("call_status is only accepted on a CALL")
        elif status not in CRM_CALL_STATUSES:
            problems.append(f"call_status {status!r} is not one of {sorted(CRM_CALL_STATUSES)}")
    duration = body.get("duration_seconds")
    if duration is not None and not isinstance(duration, int):
        problems.append("duration_seconds must be an int or absent")
    return problems

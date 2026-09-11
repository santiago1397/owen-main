"""OWEN call + message lifecycle -> the CRM's `POST /api/events` body. PURE (stdlib only).

## The contract, READ FROM THE CRM'S SOURCE, not guessed

`ghl-clone` (`backend/app/main.py::EventIngest` / `ingest_event`) accepts exactly:

    {"contact_id": int | None,                           # see THE AMENDMENT below
     "from_number": str | None,                          # alias: "caller_number"
     "type": "SMS" | "CALL" | "EMAIL" | "INTERNAL_COMMENT",
     "direction": "INBOUND" | "OUTBOUND",                # default "INBOUND"
     "body": str | None,
     "duration_seconds": int | None,
     "call_status": str | None,                          # CALL only
     "recording_url": str | None,
     "provider_ref": str | None}

and enforces these rules, which we must satisfy or be rejected with a 400/404/422:

  1. An event needs a `contact_id` OR a `from_number`, else **422**. A `contact_id` that
     does not resolve is still a **404** — naming a contact that is not there is a bug,
     not a stranger.
  2. `call_status` is accepted ONLY when `type == "CALL"`, and only from
     `{completed, no-answer, busy, voicemail, failed}` — anything else is a 400.
  3. Authorisation is `Bearer ghl_pat_...` whose token carries the `events:write` scope
     AND whose owning user is ADMIN or DISPATCHER (`auth.require_events_ingest`).

## THE AMENDMENT (CRM side, 2026-09-11) — why a stranger now gets through

`contact_id` used to be REQUIRED, and that single fact silently discarded the most
valuable event this business gets. `client.resolve_contact_id` searches the CRM's
contacts and returns None for a caller who is not in them; `api.deliver_event` then
dropped the event rather than post one the CRM would 404. A first-time roofing lead
calling the bound DID therefore reached nobody.

The CRM now accepts `from_number` (its `AliasChoices` also takes `caller_number`,
which is what this module calls the same field), matches it to a contact on the LAST
TEN DIGITS — the identity rule both systems share — and CREATES a contact when nothing
matches, firing its own new-lead automation. So every body built here carries
`from_number`, on every phase, whether or not a `contact_id` was resolved.

We still resolve a `contact_id` first and send it when we have one. It is the exact
match, it is the path the CRM's own tests pin, and it costs nothing we were not already
spending. `from_number` is the fallback that makes a failure to resolve survivable
instead of fatal — including a resolve that failed because the token lacks the `read`
scope (see `client.resolve_contact_id`), which used to drop every event outright.

DEPLOY ORDER. A CRM that predates the amendment declares `contact_id: int`, so a body
without one is a 422 there. That is a 4xx: not retryable, logged, and the event is lost
exactly as it is lost today. Nothing regresses, but the CRM half must be deployed for
the fix to have any effect.

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
    # Mapped now, for the AI-agent seam in `handler.py` that is deliberately not wired yet.
    # Without these, the day somebody connects the agent, every call it handled would report
    # as `failed` (the unknown-outcome default below) and the CRM's call report would say the
    # phone system was broken. Cheaper to name here than to debug there.
    "agent": "completed",
    "transferred": "completed",
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


def to_crm_event(facts: CallEventFacts, contact_id: int | None = None) -> dict[str, Any]:
    """Build the exact body `POST /api/events` accepts. See the module docstring for why
    the two pre-terminal phases are INTERNAL_COMMENT rather than CALL.

    `provider_ref` carries `calls.id` — the value the CRM stores as
    `Opportunity.custom_fields.owen_call_id`, which is the documented join key back here.

    `contact_id` is OPTIONAL. When OWEN resolved one it is sent and the CRM files the
    event against exactly that contact; when it did not, the body carries `from_number`
    alone and the CRM matches or creates the contact itself. The key is OMITTED rather
    than sent as null so that a body for a resolved contact is byte-for-byte what this
    function built before `from_number` existed, plus the one new field.
    """
    direction = "OUTBOUND" if str(facts.direction).lower() == "outbound" else "INBOUND"
    body: dict[str, Any] = {
        "body": summary_line(facts),
        # calls.id, on EVERY phase — the join key must not depend on which event survived.
        "provider_ref": facts.owen_call_id or facts.linkedid or None,
        # THE fix for the first-time caller. Always sent, even alongside a contact_id: it
        # costs one field and it is the only thing standing between a stranger's call and
        # a dropped event if the lookup was wrong or could not run at all.
        "from_number": facts.caller_number or None,
    }
    if contact_id is not None:
        body["contact_id"] = int(contact_id)

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
    # The CRM needs ONE of the two to know whose thread this belongs on: an explicit
    # contact_id, or a number it can match-or-create against. Neither is a 422 there.
    contact_id = body.get("contact_id")
    has_number = bool(str(body.get("from_number") or "").strip())
    if contact_id is None:
        if not has_number:
            problems.append("an event needs a contact_id or a from_number")
    elif not isinstance(contact_id, int) or isinstance(contact_id, bool):
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


# --- messages -----------------------------------------------------------------------------
# Gap 2. `push.report_call_phase` was called from three places, all of them the bound-DID
# INBOUND CALL path, so a text to the same DID reached the CRM not at all: the customer's
# message was ingested, relayed to GoHighLevel and shown in OWEN's own Inbox, and the CRM —
# the thing the owner actually works out of — never saw it.
#
# An SMS is the same `POST /api/events` endpoint with `type: "SMS"`. Nothing new is needed on
# the CRM side and nothing new is needed in the queue; this is the same shape as a call
# event, carried by the same job, down the same delivery hop.

CRM_TYPE_SMS = "SMS"


@dataclass(frozen=True)
class MessageEventFacts:
    """Everything OWEN knows about one SMS/MMS on a CRM-bound DID.

    The sibling of `CallEventFacts`, and deliberately the same shape of thing: OWEN's own
    vocabulary, carried in the `crm_report` job payload, mapped onto a specific CRM only at
    the app-side adapter.

    `owen_message_id` is `messages.id` and is THE join key. It is what this module hands the
    CRM as `provider_ref` on send (`api.send_message` answers `{"message_id": ...}`, which
    the CRM stores on its ConversationEvent), so using the same field on an inbound message
    keeps one identifier for one row on both sides of the link.
    """

    owen_message_id: str                        # messages.id
    caller_number: str = ""                     # the CUSTOMER's number, either direction
    dialed_number: str = ""                     # the bound DID
    body: str = ""
    direction: str = "inbound"
    num_media: int = 0
    provider_message_sid: str = ""              # BulkVS's own id, for support threads
    extra: dict = field(default_factory=dict)

    def as_payload(self) -> dict:
        return {
            "owen_message_id": self.owen_message_id,
            "caller_number": self.caller_number,
            "dialed_number": self.dialed_number,
            "body": self.body,
            "direction": self.direction,
            "num_media": self.num_media,
            "provider_message_sid": self.provider_message_sid,
            "extra": dict(self.extra or {}),
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "MessageEventFacts":
        p = dict(payload or {})
        try:
            num_media = int(p.get("num_media") or 0)
        except (TypeError, ValueError):
            num_media = 0
        return cls(
            owen_message_id=str(p.get("owen_message_id") or ""),
            caller_number=str(p.get("caller_number") or ""),
            dialed_number=str(p.get("dialed_number") or ""),
            body=str(p.get("body") or ""),
            direction=str(p.get("direction") or "inbound"),
            num_media=max(0, num_media),
            provider_message_sid=str(p.get("provider_message_sid") or ""),
            extra=p.get("extra") if isinstance(p.get("extra"), dict) else {},
        )


def message_body(facts: MessageEventFacts) -> str:
    """What the operator reads on the CRM thread.

    The customer's own words, VERBATIM, and nothing else — an operator deciding whether to
    send a crew reads this, and a machine-written prefix on a customer's text is the kind of
    small dishonesty that makes a thread impossible to skim.

    The one addition is an MMS note, because the CRM's event row has no media column and the
    picture of the roof IS the message. It is appended (never substituted) so the words, if
    there were any, are still the first thing on the line.
    """
    text = (facts.body or "").strip()
    if facts.num_media > 0:
        plural = "attachment" if facts.num_media == 1 else "attachments"
        note = f"[{facts.num_media} {plural} — view in OWEN]"
        return f"{text} {note}".strip()
    return text


def to_crm_message_event(facts: MessageEventFacts,
                         contact_id: int | None = None) -> dict[str, Any]:
    """One SMS/MMS as the CRM's `POST /api/events` body.

    `type: "SMS"` and NEVER a `call_status` — the CRM rejects a status on a non-CALL with a
    400, and `automations.on_inbound_call` (the missed-call auto text-back) only ever looks
    at CALL rows, so a text can never trigger it.

    An INBOUND row increments the CRM thread's unread badge, which is exactly right here:
    unlike the machine-written call notes, a customer's text IS something a person has to
    read and answer.
    """
    return {
        "type": CRM_TYPE_SMS,
        "direction": "OUTBOUND" if str(facts.direction).lower() == "outbound" else "INBOUND",
        "body": message_body(facts),
        # The customer's number in both directions — it is who the thread belongs to, which
        # is what the CRM matches (or creates) a contact on.
        "from_number": facts.caller_number or None,
        # messages.id. The same key a delivery receipt arrives with, so one OWEN row is one
        # CRM row however many ways it is touched.
        "provider_ref": facts.owen_message_id or None,
        **({"contact_id": int(contact_id)} if contact_id is not None else {}),
    }

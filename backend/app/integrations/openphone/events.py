"""OpenPhone calls and texts -> the CRM's `POST /api/events` body. PURE (stdlib only).

The sibling of `integrations/crm/events.py`, and deliberately the same shape of thing: a
frozen facts dataclass in OWEN's vocabulary, carried in the `crm_report` job payload, mapped
onto a specific CRM only at the app-side adapter.

## The contract, READ FROM THE CRM'S SOURCE, not guessed

`ghl-clone` (`backend/app/main.py::EventIngest`) accepted this before the mirror existed:

    {contact_id, from_number, type, direction, body, duration_seconds,
     call_status, recording_url, provider_ref}

and gained FIVE optional fields on the CRM's `feature/openphone-thread` branch, all
defaulting to None so a body that omits them is byte-for-byte what it was:

    occurred_at    when it HAPPENED, not when it was ingested   <- without this a backfill
                                                                   stacks 30 days of history
                                                                   at the top of the thread
    dedupe_key     UNIQUE; a repeat ingest returns the first row
    source_system  "OpenPhone" — which system carried it
    source_number  the LINE it came through, so a mixed thread is readable
    transcript     OpenPhone has already done the STT; it costs nothing to carry it

## THE ONE THAT COULD HAVE TEXTED REAL CUSTOMERS

`ghl-clone`'s `automations.on_inbound_call` queues the **missed-call auto text-back** for
any INBOUND CALL whose `duration_seconds` is <= 15. It had no notion of age, because until
now every ingested event WAS "just now" — `ConversationEvent.occurred_at` defaulted to the
ingest clock and nothing could back-date it.

A 30-day backfill changes that completely. Every short or unanswered OpenPhone call in the
window is an inbound CALL with a duration under 15 seconds, so a naive mirror would queue
one auto text-back per missed call — to real customers, about calls up to a month old, from
the BulkVS number they have never seen. Today the CRM's `LoggingTransport` would record
those instead of sending them; the CRM's own CLAUDE.md says the day two env vars are set
"every send becomes a real text", so this is a live misfire waiting for a config change.

Two independent guards, because one is never enough for something that texts people:

  1. HERE: a mirrored call is reported with its REAL `occurred_at`, and the CRM refuses to
     run the text-back rule on a call that old (`automations.MISSED_CALL_MAX_AGE_SECONDS`).
  2. `sync.py` mirrors into the CRM and NOTHING ELSE. It never calls OpenPhone's send API —
     there is no such method in `providers/openphone_client.py` and this module adds none.

Guard 1 narrows an existing rule and can only ever stop a text nobody wanted; it cannot stop
one that fires correctly today, because a live BulkVS call is seconds old when it lands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.integrations.openphone import config as op_config

# Verified against ghl-clone `main.py::CALL_STATUSES`. Sending anything else is a 400.
CRM_CALL_STATUSES = frozenset({"completed", "no-answer", "busy", "voicemail", "failed"})

CRM_TYPE_CALL = "CALL"
CRM_TYPE_SMS = "SMS"

# The label that appears on the CRM thread beside every mirrored row. One word, because it
# sits in a chip next to the phone number and "OpenPhone (mirrored, read-only)" does not fit
# — the read-only part is said once, in the composer banner, where it changes what somebody
# does next.
SOURCE_SYSTEM = "OpenPhone"

# Where the CRM serves a mirrored recording. THIS IS A PATH ON THE CRM, NOT ON OWEN — it is
# rendered straight into the browser's `<audio src>`, which resolves it against the CRM's own
# origin, so the request arrives carrying the operator's CRM session cookie and never an API
# key of any kind. The CRM then asks OWEN for the bytes over the internal network.
#
# Two repositories agreeing on a string is exactly the kind of thing that silently rots, so
# both ends pin it: `tests/test_openphone_mirror.py` here, and the CRM's own
# `test_openphone_thread.py` asserts its route is mounted at the same path.
CRM_RECORDING_PATH = "/api/openphone/recordings"

# How OpenPhone's own call status vocabulary maps onto the five words the CRM will accept.
# Anything not listed becomes None rather than a guess: the CRM renders a null as "Call"
# and the Call report counts it as unknown, which is honest. Inventing "completed" for a
# status we did not recognise would put a wrong slice on the owner's donut.
_OPENPHONE_STATUS_TO_CRM = {
    "completed": "completed",
    "answered": "completed",
    "no-answer": "no-answer",
    "noanswer": "no-answer",
    "no_answer": "no-answer",
    "missed": "no-answer",
    "busy": "busy",
    "failed": "failed",
    "canceled": "failed",
    "cancelled": "failed",
    "rejected": "failed",
    "voicemail": "voicemail",
}


def crm_call_status(raw: str | None) -> Optional[str]:
    """One of the CRM's five statuses, or None when OpenPhone said something else."""
    mapped = _OPENPHONE_STATUS_TO_CRM.get(str(raw or "").strip().lower())
    return mapped if mapped in CRM_CALL_STATUSES else None


def _iso(value) -> Optional[str]:
    """An aware-UTC ISO-8601 string, or None.

    Always ends in `+00:00`. The CRM's CLAUDE.md records a bug where exactly that suffix
    decoded as a space when concatenated into a URL — this value goes in a JSON BODY, never
    a query string, which is why it is safe to send it in full rather than mangling it.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    text = str(value).strip()
    if not text:
        return None
    # OpenPhone sends RFC-3339 with a `Z`; `fromisoformat` only learned `Z` in 3.11 and
    # this has to survive being handed a string we did not generate.
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text          # pass it through rather than dropping the timestamp entirely
    if not dt.tzinfo:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class MirroredCall:
    """One OpenPhone call, in OWEN's vocabulary.

    `external_id` is OpenPhone's own immutable call id and is THE idempotency key: it is
    what `config.dedupe_key` namespaces into the CRM's unique column, so the same call
    mirrored by a backfill and then again by a poll lands on one row.
    """

    external_id: str
    customer_number: str = ""       # the OTHER party, either direction — whose thread it is
    line_number: str = ""           # OUR OpenPhone number, shown on the thread chip
    direction: str = "incoming"
    status: str = ""
    duration_seconds: Optional[int] = None
    occurred_at: Optional[str] = None
    has_recording: bool = False
    transcript: str = ""
    summary: str = ""
    extra: dict = field(default_factory=dict)

    def as_payload(self) -> dict:
        return {
            "kind": "call",
            "external_id": self.external_id,
            "customer_number": self.customer_number,
            "line_number": self.line_number,
            "direction": self.direction,
            "status": self.status,
            "duration_seconds": self.duration_seconds,
            "occurred_at": self.occurred_at,
            "has_recording": self.has_recording,
            "transcript": self.transcript,
            "summary": self.summary,
            "extra": dict(self.extra or {}),
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "MirroredCall":
        p = dict(payload or {})
        try:
            duration = (None if p.get("duration_seconds") is None
                        else max(0, int(p["duration_seconds"])))
        except (TypeError, ValueError):
            duration = None
        return cls(
            external_id=str(p.get("external_id") or ""),
            customer_number=str(p.get("customer_number") or ""),
            line_number=str(p.get("line_number") or ""),
            direction=str(p.get("direction") or "incoming"),
            status=str(p.get("status") or ""),
            duration_seconds=duration,
            occurred_at=p.get("occurred_at") or None,
            has_recording=bool(p.get("has_recording")),
            transcript=str(p.get("transcript") or ""),
            summary=str(p.get("summary") or ""),
            extra=p.get("extra") if isinstance(p.get("extra"), dict) else {},
        )


@dataclass(frozen=True)
class MirroredMessage:
    """One OpenPhone text. Same contract as `MirroredCall`, same idempotency argument."""

    external_id: str
    customer_number: str = ""
    line_number: str = ""
    direction: str = "incoming"
    body: str = ""
    occurred_at: Optional[str] = None
    num_media: int = 0
    extra: dict = field(default_factory=dict)

    def as_payload(self) -> dict:
        return {
            "kind": "message",
            "external_id": self.external_id,
            "customer_number": self.customer_number,
            "line_number": self.line_number,
            "direction": self.direction,
            "body": self.body,
            "occurred_at": self.occurred_at,
            "num_media": self.num_media,
            "extra": dict(self.extra or {}),
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "MirroredMessage":
        p = dict(payload or {})
        try:
            num_media = max(0, int(p.get("num_media") or 0))
        except (TypeError, ValueError):
            num_media = 0
        return cls(
            external_id=str(p.get("external_id") or ""),
            customer_number=str(p.get("customer_number") or ""),
            line_number=str(p.get("line_number") or ""),
            direction=str(p.get("direction") or "incoming"),
            body=str(p.get("body") or ""),
            occurred_at=p.get("occurred_at") or None,
            num_media=num_media,
            extra=p.get("extra") if isinstance(p.get("extra"), dict) else {},
        )


def _crm_direction(raw: str | None) -> str:
    """OpenPhone says incoming/outgoing; the CRM says INBOUND/OUTBOUND.

    Anything unrecognised is INBOUND, matching the CRM's own default. Inbound is the safe
    way to be wrong: it shows the row on the customer's side of the thread and, for a call,
    is the direction the operator is expected to act on.
    """
    return "OUTBOUND" if str(raw or "").strip().lower() in {
        "outgoing", "outbound"} else "INBOUND"


def call_body(facts: MirroredCall) -> str:
    """The line an operator reads on the thread.

    Says which LINE it came through in words as well as in the `source_number` chip. The
    chip is a UI affordance and UIs get redesigned; the sentence survives a copy-paste into
    an email, which is how a roofer actually forwards "here is what happened with this
    customer".
    """
    direction = "Outbound" if _crm_direction(facts.direction) == "OUTBOUND" else "Inbound"
    bits = [f"{direction} OpenPhone call"]
    if facts.line_number:
        bits.append(f"on {facts.line_number}")
    line = " ".join(bits) + "."

    tail: list[str] = []
    status = crm_call_status(facts.status)
    if status:
        tail.append(f"Outcome: {status}.")
    elif facts.status:
        # Unmapped, so it is NOT going in `call_status` — but the operator should still see
        # the word OpenPhone used rather than a silently blank outcome.
        tail.append(f"Outcome: {facts.status} (not a CRM status).")
    if facts.duration_seconds is not None:
        tail.append(f"Duration: {facts.duration_seconds}s.")
    if facts.summary.strip():
        tail.append(facts.summary.strip())
    tail.append("Mirrored from OpenPhone — reply from the CRM goes out on the BulkVS line.")
    return " ".join([line, *tail]).strip()


def message_body(facts: MirroredMessage) -> str:
    """The customer's own words, VERBATIM.

    No machine-written prefix, for the reason `crm/events.message_body` gives: a thread with
    a label on every line is a thread nobody skims. WHICH line it arrived on is carried in
    `source_number`, where the UI can render it small and grey instead of in the sentence.

    The one addition is an MMS note, because the CRM's event row has no media column and the
    mirror deliberately does not copy media — see `sync.py`.
    """
    text = (facts.body or "").strip()
    if facts.num_media > 0:
        plural = "attachment" if facts.num_media == 1 else "attachments"
        return f"{text} [{facts.num_media} {plural} — view in OpenPhone]".strip()
    return text


def _common(facts, contact_id: int | None, kind: str) -> dict[str, Any]:
    """The fields every mirrored body carries, whatever it is."""
    body: dict[str, Any] = {
        # The CUSTOMER's number in both directions — it is whose thread this is, and what
        # the CRM matches (or creates) a contact on, on the last ten digits.
        "from_number": facts.customer_number or None,
        # OpenPhone's own id, so an operator can find the same row in OpenPhone itself.
        "provider_ref": facts.external_id or None,
        # THE idempotency key. UNIQUE on the CRM side: a second ingest of this exact call or
        # text returns the row that already exists instead of writing a second one.
        "dedupe_key": op_config.dedupe_key(kind, facts.external_id),
        # WHEN IT HAPPENED. Without this the CRM stamps the ingest clock and a 30-day
        # backfill lands as 30 days of history all dated today, in poll order — which is the
        # opposite of the one-timeline-per-customer this feature exists to produce.
        "occurred_at": _iso(facts.occurred_at),
        "source_system": SOURCE_SYSTEM,
        "source_number": facts.line_number or None,
    }
    if contact_id is not None:
        body["contact_id"] = int(contact_id)
    return body


def to_crm_call_event(facts: MirroredCall,
                      contact_id: int | None = None) -> dict[str, Any]:
    """One mirrored call as the CRM's `POST /api/events` body.

    ONE event per call, not the three the live BulkVS path sends. That is not a shortcut —
    it is the truth: the mirror observes a call that has already finished, so reporting it
    as "started" and then "answered" would be inventing a lifecycle it never watched. The
    CRM renders a single CALL row, which is what a mirrored history should look like.
    """
    body = _common(facts, contact_id, "call")
    body["type"] = CRM_TYPE_CALL
    body["direction"] = _crm_direction(facts.direction)
    body["body"] = call_body(facts)
    if facts.duration_seconds is not None:
        body["duration_seconds"] = int(facts.duration_seconds)
    status = crm_call_status(facts.status)
    if status:
        # Only ever sent on a CALL, and only from the five the CRM accepts — anything else
        # is a 400 there, which would dead-letter the job after five attempts.
        body["call_status"] = status
    if facts.has_recording and facts.external_id:
        # A path on the CRM. See CRM_RECORDING_PATH: the audio streams CRM -> OWEN ->
        # OpenPhone, and the OpenPhone key never leaves OWEN's app container.
        body["recording_url"] = f"{CRM_RECORDING_PATH}/{facts.external_id}"
    if facts.transcript.strip():
        body["transcript"] = facts.transcript.strip()
    return body


def to_crm_message_event(facts: MirroredMessage,
                         contact_id: int | None = None) -> dict[str, Any]:
    """One mirrored text as the CRM's `POST /api/events` body.

    `type: "SMS"` and NEVER a `call_status` — the CRM rejects a status on a non-CALL with a
    400, and its missed-call rule only ever looks at CALL rows, so a mirrored text cannot
    trigger an automation however old it is.
    """
    body = _common(facts, contact_id, "message")
    body["type"] = CRM_TYPE_SMS
    body["direction"] = _crm_direction(facts.direction)
    body["body"] = message_body(facts)
    return body


def to_crm_event(payload: dict, contact_id: int | None = None) -> dict[str, Any]:
    """Dispatch on the payload's own `kind`, for the worker adapter which gets a dict.

    Unknown kinds raise rather than defaulting: a payload shape we do not recognise is a
    deploy-order mistake, and guessing would file it on a customer's timeline as the wrong
    sort of thing.
    """
    kind = str((payload or {}).get("kind") or "").strip().lower()
    if kind == "call":
        return to_crm_call_event(MirroredCall.from_payload(payload), contact_id)
    if kind == "message":
        return to_crm_message_event(MirroredMessage.from_payload(payload), contact_id)
    raise ValueError(f"unknown mirrored payload kind {kind!r}")

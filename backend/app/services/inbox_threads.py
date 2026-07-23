"""Pure per-CONTACT thread merging for the Quo-style Inbox.

Unlike the legacy Messages inbox (one thread per (number_id, caller_id) —
services/message_threads.py), the Inbox threads by CONTACT: one thread per caller across
every BulkVS DID, with that contact's messages AND calls folded into a single summary.

Read/open state is stored per contact (contact_thread_state) but the interesting bits are
DERIVED here so the webhooks never have to write state:
  - unread  = inbound activity newer than last_read_at (NULL last_read_at => everything unread)
  - open    = closed_at is NULL, or there is activity NEWER than closed_at (auto-reopen)
  - responded = the newest message is not an unanswered inbound

From-number ("sticky DID") resolution also lives here, pure:
  - calls  go out from the DID this contact last interacted with, else the global default;
  - SMS    goes out from the sticky DID when it passes the 10DLC gate, else FALLS BACK to the
    global default DID (when that one passes), else sending is blocked with a reason.

Kept import-light (stdlib + app.services.sms which is stdlib-only) so it's unit-testable
without sqlalchemy / a database.
"""

from dataclasses import dataclass, field
from datetime import datetime

from app.services import sms

# ---------------------------------------------------------------------------------------
# Inputs (duck-typed). Rows only need the attributes below.
#   message rows: caller_id, direction, body, received_at, number_id, number_phone,
#                 sms_enabled, sms_campaign_id, caller_number
#   call rows:    caller_id, direction, status, started_at, duration_seconds, number_id,
#                 number_phone, sms_enabled, sms_campaign_id, caller_number
# ---------------------------------------------------------------------------------------


@dataclass
class DidRef:
    """One of OUR numbers as seen from an activity row (enough to run the 10DLC gate)."""

    number_id: str | None = None
    phone_number: str | None = None
    sms_enabled: bool = False
    sms_campaign_id: str | None = None


@dataclass
class ContactThread:
    caller_id: str
    caller_number: str | None = None
    last_at: datetime | None = None
    last_kind: str | None = None  # 'message' | 'call'
    last_direction: str | None = None
    last_preview: str | None = None
    message_count: int = 0
    call_count: int = 0
    unread_count: int = 0
    open: bool = True
    responded: bool = True
    sticky: DidRef = field(default_factory=DidRef)


def _call_preview(direction: str | None, status: str | None) -> str:
    if direction == "outbound":
        return "↗ You called"
    if (status or "").lower() in ("no-answer", "busy", "failed", "canceled"):
        return "↙ Missed call"
    return "↙ Call"


def merge_threads(
    message_rows,
    call_rows,
    states: dict[str, tuple[datetime | None, datetime | None]] | None = None,
) -> list[ContactThread]:
    """Fold message + call rows (any order) into per-contact summaries, newest-first.

    `states` maps caller_id -> (last_read_at, closed_at); missing key = never read/closed.
    Rows without a caller_id are skipped — a contact identity is the thread key.
    """
    states = states or {}
    threads: dict[str, ContactThread] = {}
    # newest message per contact, to derive `responded`
    newest_msg: dict[str, tuple[datetime | None, str | None]] = {}

    def bump(cid: str, r, kind: str, at: datetime | None, preview: str) -> ContactThread:
        t = threads.get(cid)
        if t is None:
            t = threads[cid] = ContactThread(caller_id=cid)
        if t.caller_number is None:
            t.caller_number = getattr(r, "caller_number", None)
        if at is not None and (t.last_at is None or at > t.last_at):
            t.last_at = at
            t.last_kind = kind
            t.last_direction = getattr(r, "direction", None)
            t.last_preview = preview
            t.sticky = DidRef(
                number_id=str(r.number_id) if getattr(r, "number_id", None) else None,
                phone_number=getattr(r, "number_phone", None),
                sms_enabled=bool(getattr(r, "sms_enabled", False)),
                sms_campaign_id=getattr(r, "sms_campaign_id", None),
            )
        return t

    for r in message_rows:
        if r.caller_id is None:
            continue
        cid = str(r.caller_id)
        at = getattr(r, "received_at", None)
        t = bump(cid, r, "message", at, r.body or "")
        t.message_count += 1
        prev = newest_msg.get(cid)
        if at is not None and (prev is None or prev[0] is None or at > prev[0]):
            newest_msg[cid] = (at, getattr(r, "direction", None))
        last_read = states.get(cid, (None, None))[0]
        if getattr(r, "direction", None) != "outbound" and (
            last_read is None or (at is not None and at > last_read)
        ):
            t.unread_count += 1

    for r in call_rows:
        if r.caller_id is None:
            continue
        cid = str(r.caller_id)
        at = getattr(r, "started_at", None)
        t = bump(cid, r, "call", at, _call_preview(getattr(r, "direction", None), getattr(r, "status", None)))
        t.call_count += 1
        last_read = states.get(cid, (None, None))[0]
        if getattr(r, "direction", None) != "outbound" and (
            last_read is None or (at is not None and at > last_read)
        ):
            t.unread_count += 1

    for cid, t in threads.items():
        _, closed_at = states.get(cid, (None, None))
        # auto-reopen: activity newer than the close wins
        t.open = closed_at is None or (t.last_at is not None and t.last_at > closed_at)
        nm = newest_msg.get(cid)
        t.responded = nm is None or nm[1] == "outbound"

    return sorted(
        threads.values(),
        key=lambda t: (t.last_at is not None, t.last_at),
        reverse=True,
    )


def resolve_sms_from(
    sticky: DidRef | None, default: DidRef | None
) -> tuple[DidRef | None, bool, str | None]:
    """Pick the DID an SMS reply goes out from: sticky when it passes the 10DLC gate, else
    the global default (fallback=True), else (None, False, reason). Pure."""
    if sticky and sticky.number_id and sms.outbound_block_reason(
        sticky.sms_enabled, sticky.sms_campaign_id
    ) is None:
        return sticky, False, None
    if default and default.number_id and sms.outbound_block_reason(
        default.sms_enabled, default.sms_campaign_id
    ) is None:
        return default, True, None
    reason = None
    if sticky and sticky.number_id:
        reason = sms.outbound_block_reason(sticky.sms_enabled, sticky.sms_campaign_id)
    return None, False, reason or "no SMS-enabled number available for this contact"


def resolve_call_from(sticky: DidRef | None, default: DidRef | None) -> DidRef | None:
    """Pick the caller-ID DID for an outbound call: sticky first, else the default. Pure —
    ownership validation (owned BulkVS DID) stays with the telephony endpoint."""
    if sticky and sticky.number_id and sticky.phone_number:
        return sticky
    if default and default.number_id and default.phone_number:
        return default
    return None

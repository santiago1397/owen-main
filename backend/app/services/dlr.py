"""Carrier delivery receipts: find the message they belong to, and apply them (2026-09-16).

`providers/bulkvs.parse_delivery_receipt` decides WHAT a payload is. This decides WHICH
outbound message it is about, which is the hard half, and then advances that row and tells
the CRM.

## Why correlation is not simply "look up the id"

It should have been. `/messageSend` answers with a `RefId`, `/webhooks/bulkvs/message-status`
matches on `bulkvs-<RefId>`, and every other carrier on this platform works that way.

**Measured on production, 2026-09-16: the DLR's `id` is a different identifier from the
send's `RefId`.** A send answered `RefId 4551F89F`; the receipt for it arrived carrying
`id:1162999967`. Hex versus decimal, eight characters versus ten — they are not the same
number in two renderings, they are two different identifier spaces. Nothing here joins on
either one, and a future reader who "fixes" this by matching `provider_message_sid` will
silently receipt nothing.

## What it correlates on instead, exactly

Four facts, all of which the receipt carries and the outbound row also holds:

1. **our DID** — the receipt's `To` is the number the text was sent FROM;
2. **the recipient** — the receipt's `From` is the number it was sent TO. (Both are
   inverted relative to a normal inbound message, because a receipt is addressed to us
   about them.) Both are compared on the LAST TEN DIGITS, the identity rule this platform
   uses everywhere;
3. **the text prefix** — `text:` is a truncated echo of what was sent. The outbound body
   must START WITH it, after the trailing ellipsis a carrier adds is removed. Compared
   case-insensitively, because the echo is not guaranteed to preserve case;
4. **the submit time** — `submit date:` is `YYMMDDhhmm[ss]` on *the SMSC's own clock, in a
   timezone the receipt does not state*. So it is NOT treated as authoritative: it only
   has to fall within `_WINDOW` of the row's `received_at`, which is wide enough to absorb
   any plausible offset, and it breaks ties by nearness.

An empty `text:` drops fact 3 and leans on the other three. A receipt matching NOTHING is
kept and reported rather than thrown away — see `webhooks/bulkvs.py`.

**Ambiguity is resolved, never guessed at.** Among candidates, a row that has not been
receipted yet wins over one that has; then the nearest submit time; then the most recent.
The same text sent twice to the same person within the window is the case this can get
wrong, and it is recorded in DECISIONS.md rather than hidden.

## Idempotency

Every applied receipt is remembered on the outbound row's `raw_payload` as
`bulkvs_dlr_seen` (a list of `"<id>:<stat>"`). A carrier re-POSTing the same receipt — which
BulkVS does when it does not get a 200 — changes nothing the second time, and the backfill
command can be run as often as anyone likes.

`raw_payload` is JSONB and this writes new KEYS beside the CRM-link marker rather than over
it: the marker is the only thing that says a message was the CRM's, and losing it would
stop that message's receipts reaching the CRM for ever.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Message
from app.providers.bulkvs import DeliveryReceipt

logger = logging.getLogger("services.dlr")

# How far the receipt's stated submit time may be from the row's `received_at`. Deliberately
# generous: the SMSC's timezone is not stated on the receipt, so anything tighter than a day
# would silently drop real receipts from a carrier in another zone. It is a sanity bound and
# a tie-break, never an identity check.
_WINDOW = timedelta(days=2)

# How far back to look at all, when the submit date cannot be parsed.
_FALLBACK_WINDOW = timedelta(days=7)

# The carrier's word, mapped onto the platform's own outbound status vocabulary — the same
# keys as `services/sms.OUTBOUND_STATUS_RANK`, so the existing forward-only ladder applies
# unchanged and the CRM (which took its vocabulary from ours on purpose) needs no
# translation either.
STAT_TO_STATUS = {
    "DELIVRD": "delivered",
    "ACCEPTD": "sent",       # accepted by the handset's network; not yet delivered
    "UNDELIV": "undelivered",
    "REJECTD": "failed",
    "EXPIRED": "failed",
    "DELETED": "failed",
    "UNKNOWN": "failed",
}

# What the operator reads under the bubble in the CRM. A carrier word is not a sentence, and
# "UNDELIV err:255" tells a roofer nothing about whether to try another number.
STAT_SENTENCE = {
    "UNDELIV": "the carrier could not deliver it",
    "REJECTD": "the carrier rejected it",
    "EXPIRED": "the carrier gave up trying to deliver it",
    "DELETED": "the carrier deleted it before delivering it",
    "UNKNOWN": "the carrier does not know what happened to it",
}


def status_for(receipt: DeliveryReceipt) -> str:
    """The platform status this receipt means. Unknown carrier words become `failed`.

    Deliberately pessimistic for a word we do not know: the owner's question is "did the
    text arrive", and answering "probably" for an unrecognised code is the one answer that
    is never useful.
    """
    return STAT_TO_STATUS.get(receipt.stat.upper(), "failed")


def detail_for(receipt: DeliveryReceipt) -> str:
    """The sentence shown beneath the message, or "" when nothing needs explaining.

    The carrier's error code is carried verbatim and NOT interpreted: BulkVS's codes are
    per-carrier and undocumented for this account, so "(error 255)" is an honest pointer to
    something a human can ask BulkVS about, while inventing a meaning for it would not be.
    """
    stat = receipt.stat.upper()
    if stat == "DELIVRD":
        return ""
    sentence = STAT_SENTENCE.get(stat, "the carrier reported %s" % stat)
    err = (receipt.err or "").strip()
    if err and err.strip("0"):          # "000" and "0" both mean nothing went wrong
        sentence += " (error %s)" % err.lstrip("0")
    return sentence


def _digits(value: str | None) -> str:
    return "".join(c for c in str(value or "") if c.isdigit())


def _key(value: str | None) -> str:
    """The last ten digits — the identity rule shared with the CRM, the allowlist and the
    number sync. Two systems disagreeing about whether two renderings are one line is how a
    receipt lands on the wrong message."""
    d = _digits(value)
    return d[-10:] if len(d) >= 10 else d


def submit_instant(receipt: DeliveryReceipt) -> datetime | None:
    """`submit date` as a UTC datetime, or None.

    **The timezone is a guess and is treated as one.** SMPP receipts carry local SMSC time
    with no offset; UTC is assumed only so the value can be compared at all, and every use
    of the result is bounded by `_WINDOW` precisely because the assumption may be hours out.
    """
    raw = (receipt.submit_date or "").strip()
    for fmt, size in (("%y%m%d%H%M%S", 12), ("%y%m%d%H%M", 10)):
        if len(raw) == size:
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                return None
    return None


def echo_prefix(receipt: DeliveryReceipt) -> str:
    """The `text:` echo, with the carrier's truncation marker removed."""
    text = (receipt.text_prefix or "").strip()
    while text.endswith("."):
        text = text[:-1]
    return text.strip()


@dataclass
class Correlation:
    """Which message a receipt belongs to, and how sure we are."""

    message: Message | None
    how: str                      # a sentence for the log and the backfill report
    candidates: int = 0

    @property
    def matched(self) -> bool:
        return self.message is not None


async def correlate(db, receipt: DeliveryReceipt) -> Correlation:
    """Find the outbound `messages` row this receipt is about. See the module docstring."""
    did, customer = _key(receipt.dialed_number), _key(receipt.customer_number)
    if not did or not customer:
        return Correlation(None, "the receipt names no DID or no recipient")

    submitted = submit_instant(receipt)
    window = _WINDOW if submitted else _FALLBACK_WINDOW
    anchor = submitted or datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(Message)
            .where(Message.direction == "outbound",
                   Message.received_at >= anchor - window,
                   Message.received_at <= anchor + window)
            .order_by(Message.received_at.desc())
        )
    ).scalars().all()

    prefix = echo_prefix(receipt).lower()
    candidates = [
        m for m in rows
        if _key(m.from_number) == did and _key(m.to_number) == customer
        and (not prefix or (m.body or "").strip().lower().startswith(prefix))
    ]
    if not candidates:
        return Correlation(None, "no outbound message from %s to that number matches the "
                                 "receipt's text and time" % receipt.dialed_number)

    # Already receipted with this exact id and outcome: nothing to do, and saying so is
    # what makes the backfill safe to run again.
    seen_marker = "%s:%s" % (receipt.receipt_id, receipt.stat.upper())
    for m in candidates:
        if seen_marker in _seen(m):
            return Correlation(m, "already applied", len(candidates))

    def nearness(m: Message):
        fresh = 0 if not _seen(m) else 1          # an unreceipted row wins
        if submitted is None or m.received_at is None:
            return (fresh, timedelta(0))
        at = m.received_at
        if at.tzinfo is None:                      # SQLite round-trips naive; Postgres does not
            at = at.replace(tzinfo=timezone.utc)
        return (fresh, abs(at - submitted))

    best = sorted(candidates, key=nearness)[0]
    how = ("matched on the DID, the recipient, the %s and the submit time"
           % ("text echo" if prefix else "recipient alone (the receipt echoed no text)"))
    if len(candidates) > 1:
        how += " — %d messages matched, the nearest in time was taken" % len(candidates)
    return Correlation(best, how, len(candidates))


def _seen(message: Message) -> list:
    raw = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    seen = raw.get("bulkvs_dlr_seen")
    return list(seen) if isinstance(seen, list) else []


def remember(message: Message, receipt: DeliveryReceipt) -> None:
    """Record the receipt on the outbound row, beside whatever is already there.

    A NEW DICT is assigned rather than mutated in place: SQLAlchemy does not track mutation
    inside a JSONB value, and an in-place edit is the classic way to write nothing at all.
    """
    raw = dict(message.raw_payload or {})
    seen = list(raw.get("bulkvs_dlr_seen") or [])
    marker = "%s:%s" % (receipt.receipt_id, receipt.stat.upper())
    if marker not in seen:
        seen.append(marker)
    raw["bulkvs_dlr_seen"] = seen
    raw["bulkvs_dlr"] = {
        "id": receipt.receipt_id,
        "stat": receipt.stat,
        "err": receipt.err,
        "submit_date": receipt.submit_date,
        "done_date": receipt.done_date,
        # THE WHOLE PAYLOAD. Nobody here could read a stored one, so which field (if any)
        # BulkVS uses to flag a receipt is still unknown — this is where the next live one
        # answers it. See providers/bulkvs._DLR_FLAG_KEYS.
        "raw": receipt.raw,
    }
    message.raw_payload = raw


async def apply(db, receipt: DeliveryReceipt, *, relay: bool = True) -> dict:
    """Correlate, advance the outbound row, and relay the receipt to the CRM.

    Returns a dict for the caller to log or count. Never raises for an ordinary failure to
    correlate: a receipt we cannot place is a fact to report, not an exception that costs
    the webhook its 200 and makes BulkVS re-deliver for ever.
    """
    from app.services import sms

    found = await correlate(db, receipt)
    if not found.matched:
        logger.warning("bulkvs dlr %s (%s): %s", receipt.receipt_id, receipt.stat, found.how)
        return {"applied": False, "reason": found.how, "message_id": None}

    msg = found.message
    if found.how == "already applied":
        return {"applied": False, "reason": "already applied", "message_id": str(msg.id),
                "status": msg.status}

    status = status_for(receipt)
    before = msg.status
    msg.status = sms.advance_status(before, status)
    remember(msg, receipt)
    await db.commit()
    logger.info("bulkvs dlr %s: message %s %s -> %s (%s)",
                receipt.receipt_id, msg.id, before, msg.status, found.how)

    relayed = False
    if relay:
        # Total, opt-in and marker-gated: it does nothing at all unless the CRM itself sent
        # this message, so an operator's own text from the Inbox is never relayed.
        from app.integrations.crm import hook as crm_hook

        relayed = await crm_hook.handle_delivery_receipt(
            message_id=str(msg.id), status=status, detail=detail_for(receipt))
    return {"applied": True, "reason": found.how, "message_id": str(msg.id),
            "status": msg.status, "was": before, "relayed_to_crm": relayed,
            "candidates": found.candidates}

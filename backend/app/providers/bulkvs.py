"""BulkVS inbound-SMS/MMS adapter + source-IP verification helpers (Ticket 09).

BulkVS delivers inbound (mobile-originated) messages by POSTing JSON to a webhook URL, and
— unlike Twilio/SignalWire — it does NOT sign the request with an HMAC. Instead the webhook
is authenticated by SOURCE IP: BulkVS only originates these callbacks from a fixed set of
addresses (see BULKVS_INBOUND_IPS). The IP helpers here are consumed by the shared webhook
verifier (app/webhooks/common.py); the message parser normalizes a payload into the same
NormalizedMessageEvent the existing inbound-SMS path already ingests.

Kept import-light on purpose (only stdlib + providers.base) so the parse + IP logic is unit
-testable in a bare sandbox without httpx / pydantic / sqlalchemy.

Payload field-name ASSUMPTIONS (BulkVS "Message" MO webhook — confirm against a live sample):
  From    -> sender E.164/NANP digits
  To      -> receiving DID; MAY arrive as a JSON array (["1xxxxxxxxxx"]) or a bare string
  Message -> the text body (alias: Body)
  Attachments / MediaURLs -> MMS media URLs (array; stored as-is)
BulkVS carries NO provider message SID, so we SYNTHESIZE one deterministically as
  sha256(from | to | body | timestamp)
which keeps the upsert-on-SID idempotent across webhook retries of the same message. If the
payload carries no timestamp-ish field the timestamp segment is empty (two byte-identical
texts would then collapse to one row — acceptable for the inbox).
"""

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import unquote_plus

from app.providers.base import NormalizedMessageEvent

# BulkVS inbound (MO) SMS/MMS source IPs. A request from any other address is rejected.
BULKVS_INBOUND_IPS: tuple[str, ...] = ("52.206.134.245", "192.9.236.42")


def client_ip(x_forwarded_for: str | None, peer: str | None) -> str:
    """Resolve the real client IP. Behind Traefik the TCP peer is the proxy, so the original
    caller is the LEFTMOST entry of X-Forwarded-For; fall back to the TCP peer when no XFF."""
    if x_forwarded_for:
        first = x_forwarded_for.split(",")[0].strip()
        if first:
            return first
    return (peer or "").strip()


def ip_allowed(ip: str, allowlist) -> bool:
    return bool(ip) and ip in tuple(allowlist)


def _to_e164(tn: str) -> str:
    """Normalize a BulkVS number to E.164 (mirrors bulkvs_client._to_e164; duplicated here to
    keep this module import-light / test-friendly). BulkVS reports bare NANP digits."""
    raw = (tn or "").strip()
    if raw.startswith("+"):
        digits = "".join(c for c in raw[1:] if c.isdigit())
        return f"+{digits}" if digits else raw
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}" if digits else raw


def _first(v):
    """BulkVS may send `To` as a single-element array; take the first non-empty element."""
    if isinstance(v, (list, tuple)):
        return next((x for x in v if x), None)
    return v


def _media_list(params: dict) -> list[str]:
    raw = (
        params.get("Attachments")
        or params.get("MediaURLs")
        or params.get("MediaUrls")
        or params.get("Media")
        or []
    )
    if isinstance(raw, str):
        raw = [raw] if raw else []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(m) for m in raw if m]


_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


def _decode_body(raw):
    """BulkVS form-urlencodes the `Message` field INSIDE its JSON payload.

    Confirmed against a live MO webhook (2026-09-09), which delivered:
        "Hi%2C+I+got+your+%23+on+Google+Biz+as+a+roofing+pro..."
    so every inbound SMS was stored — and displayed — with %2C for commas and + for spaces.

    Guarded rather than decoded unconditionally, because `unquote_plus` turns '+' into a
    space: if BulkVS ever sends a plain body, "call me + I'll answer" would be quietly
    mangled. A form-encoded body has had its spaces replaced by '+', so a body CONTAINING a
    space was never encoded and is returned untouched.
    """
    if not isinstance(raw, str) or not raw or " " in raw:
        return raw
    if "+" not in raw and not _PERCENT_ESCAPE.search(raw):
        return raw          # nothing encoded to undo (a single word, "STOP", …)
    try:
        return unquote_plus(raw)
    except Exception:       # noqa: BLE001 - an undecodable body is kept verbatim, never dropped
        return raw


# --- DELIVERY RECEIPTS ARRIVE ON THIS SAME WEBHOOK (2026-09-16) ---------------------------
#
# MEASURED ON PRODUCTION, the morning after texting went live. BulkVS does not only POST
# mobile-originated messages to the MO webhook — it posts DELIVERY RECEIPTS there too, as an
# ordinary-looking inbound message whose `From` is the RECIPIENT and whose `Message` is an
# SMPP `deliver_sm` receipt:
#
#   id:1162999967 sub:001 dlvrd:000 submit date:2609160247 done date:2609160247
#   stat:UNDELIV err:255 text:Dream Te...
#
# Every one of those was stored as an inbound text and relayed onward, so the CRM showed a
# customer's own thread containing two messages the customer never wrote. That is the bug
# this block exists to stop, and the receipt is thrown away twice over: once as information
# we badly wanted (did the text arrive?) and once as noise in a customer's record.
#
# THE MATCH IS DELIBERATELY STRICT — all seven fields, in order, anchored at the start. The
# guard that matters is "a real customer text must never be mistaken for a receipt", and a
# person cannot type this by accident. `text:` is optional because the field is a truncated
# echo and a carrier may omit it.
#
# WHAT THE RAW PAYLOAD FLAGS IT WITH IS NOT KNOWN. Production could not be read from here,
# so detection is on the BODY, which is definitive, and `_DLR_FLAG_KEYS` below is a
# defensive second route for a payload that says so outright. Every receipt we recognise
# keeps its whole raw payload (see services/dlr.py), so the next live one writes the answer
# down instead of it being guessed at again.

_DLR = re.compile(
    r"^\s*id:(?P<id>\S+)"
    r"\s+sub:(?P<sub>\d+)"
    r"\s+dlvrd:(?P<dlvrd>\d+)"
    r"\s+submit\s+date:(?P<submit_date>\d{8,14})"
    r"\s+done\s+date:(?P<done_date>\d{8,14})"
    r"\s+stat:(?P<stat>[A-Za-z]+)"
    r"\s+err:(?P<err>\w+)"
    r"(?:\s+text:(?P<text>.*))?$",
    re.DOTALL,
)

# Looks like a receipt but did not match — a carrier variant we have not seen. Logged rather
# than silently stored, so an unrecognised shape is visible instead of landing in a
# customer's thread the way the first one did.
_DLR_ISH = re.compile(r"^\s*id:\S+.*\bstat:", re.IGNORECASE | re.DOTALL)

# A payload that declares itself. Checked as a SECOND route, never instead of the body: the
# names are plausible rather than observed, and a wrong guess here must not be able to
# reclassify a customer's text.
_DLR_FLAG_KEYS = ("MessageType", "messageType", "Type", "type", "Category", "category",
                  "EsmClass", "esm_class", "esmClass")
_DLR_FLAG_VALUES = ("dlr", "receipt", "delivery", "delivery_receipt", "deliveryreceipt",
                    "status", "deliver_sm")


def flagged_as_receipt(params: dict) -> str:
    """The key that declares this payload a delivery receipt, or "".

    Returns the KEY rather than a bool so the caller can log which field said so — the one
    fact about this webhook nobody here has been able to observe.
    """
    if not isinstance(params, dict):
        return ""
    for key in _DLR_FLAG_KEYS:
        value = params.get(key)
        if isinstance(value, str) and value.strip().lower() in _DLR_FLAG_VALUES:
            return key
    return ""


@dataclass(frozen=True)
class DeliveryReceipt:
    """One carrier delivery receipt, as the MO webhook delivered it.

    `receipt_id` is the SMPP message id and is **NOT** the RefId `/messageSend` returns —
    measured: RefId `4551F89F` against DLR id `1162999967`. They are different identifier
    spaces, so nothing here correlates on it; see `services/dlr.py` for what does.
    """

    receipt_id: str
    stat: str                 # DELIVRD | UNDELIV | REJECTD | EXPIRED | DELETED | ...
    err: str                  # "000" when nothing went wrong
    submit_date: str          # YYMMDDhhmm[ss], the SMSC's own clock and timezone
    done_date: str
    text_prefix: str          # a truncated echo of the message that was sent
    delivered: int            # the `dlvrd:` counter
    submitted: int            # the `sub:` counter
    customer_number: str      # the DLR's From — the RECIPIENT of the original text
    dialed_number: str        # the DLR's To — our own DID
    raw: dict = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.stat.upper() != "DELIVRD"


def parse_delivery_receipt(params: dict) -> "DeliveryReceipt | None":
    """A `DeliveryReceipt` if this MO payload is one, else None. Pure — no DB, no HTTP.

    None means "treat it as an ordinary inbound message", which is what every real text
    gets, so a parser that is too shy costs nothing that is not already lost today.
    """
    if not isinstance(params, dict):
        return None
    raw_body = (
        params.get("Message") or params.get("Body")
        or params.get("message") or params.get("body")
    )
    body = _decode_body(raw_body)
    if not isinstance(body, str) or not body:
        return None
    m = _DLR.match(body)
    if m is None:
        return None

    frm = _first(params.get("From") or params.get("from"))
    to = _first(params.get("_tracking_number") or params.get("To") or params.get("to"))
    text = (m.group("text") or "").strip()
    return DeliveryReceipt(
        receipt_id=m.group("id"),
        stat=m.group("stat").upper(),
        err=m.group("err"),
        submit_date=m.group("submit_date"),
        done_date=m.group("done_date"),
        text_prefix=text,
        delivered=int(m.group("dlvrd")),
        submitted=int(m.group("sub")),
        customer_number=_to_e164(str(frm)) if frm else "",
        dialed_number=_to_e164(str(to)) if to else "",
        raw=dict(params),
    )


def looks_like_unparsed_receipt(params: dict) -> bool:
    """A body shaped like a receipt that `parse_delivery_receipt` could not read.

    Exists to make a carrier variant LOUD. The first one of these cost two junk messages in
    a customer's CRM thread precisely because nothing was watching for the shape.
    """
    if not isinstance(params, dict):
        return False
    body = _decode_body(
        params.get("Message") or params.get("Body")
        or params.get("message") or params.get("body")
    )
    if not isinstance(body, str) or not body:
        return False
    return bool(_DLR_ISH.match(body)) and _DLR.match(body) is None


class BulkvsAdapter:
    """Only inbound messaging is modelled — BulkVS has no status/recording webhooks here."""

    name = "bulkvs"

    def parse_message_event(self, params: dict) -> NormalizedMessageEvent:
        frm = _first(params.get("From") or params.get("from"))
        # Trust the tracking-number query override we control (webhooks/bulkvs.py) over the
        # payload's To, mirroring the Twilio/SignalWire handling.
        to = _first(params.get("_tracking_number") or params.get("To") or params.get("to"))
        raw_body = (
            params.get("Message")
            or params.get("Body")
            or params.get("message")
            or params.get("body")
        )
        body = _decode_body(raw_body)
        media_urls = _media_list(params)
        timestamp = str(
            params.get("Timestamp")
            or params.get("timestamp")
            or params.get("Date")
            or params.get("RefId")
            or params.get("RefID")
            or ""
        )

        from_e = _to_e164(str(frm)) if frm else None
        to_e = _to_e164(str(to)) if to else None
        # Hashed on the RAW body, not the decoded one: the SID is the idempotency key, and
        # re-deriving it from decoded text would make every already-stored message look new
        # if BulkVS ever redelivered it.
        sid = "bulkvs-" + hashlib.sha256(
            f"{from_e or ''}|{to_e or ''}|{raw_body or ''}|{timestamp}".encode()
        ).hexdigest()

        return NormalizedMessageEvent(
            provider_message_sid=sid,
            from_number=from_e,
            to_number=to_e,
            body=body,
            status="received",
            num_media=len(media_urls),
            media_urls=media_urls,
            direction="inbound",
            raw=dict(params) if isinstance(params, dict) else {},
        )

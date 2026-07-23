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


class BulkvsAdapter:
    """Only inbound messaging is modelled — BulkVS has no status/recording webhooks here."""

    name = "bulkvs"

    def parse_message_event(self, params: dict) -> NormalizedMessageEvent:
        frm = _first(params.get("From") or params.get("from"))
        # Trust the tracking-number query override we control (webhooks/bulkvs.py) over the
        # payload's To, mirroring the Twilio/SignalWire handling.
        to = _first(params.get("_tracking_number") or params.get("To") or params.get("to"))
        body = (
            params.get("Message")
            or params.get("Body")
            or params.get("message")
            or params.get("body")
        )
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
        sid = "bulkvs-" + hashlib.sha256(
            f"{from_e or ''}|{to_e or ''}|{body or ''}|{timestamp}".encode()
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

"""Retell's request signature — PURE (stdlib only), so it is testable with nothing installed.

Every webhook and every custom-function call Retell makes carries

    X-Retell-Signature: v=<unix ms>,d=<hex>
    hex = HMAC-SHA256(key = RETELL_API_KEY, message = <raw request body> + <unix ms>)

(the scheme Retell's own SDKs implement in `Retell.verify`). Three rules, each a refusal:

  * **The RAW body.** The bytes as received, never a re-serialisation: `json.dumps` of the
    parsed body reorders nothing today and changes spacing, escaping and float formatting
    tomorrow. A request whose body was re-serialised fails, and a test pins that.
  * **A five-minute window.** A captured request replayed later is refused even though its
    signature is genuine.
  * **Constant-time comparison** (`hmac.compare_digest`).

Refusals are returned as sentences, not raised, so the route decides the status code (401)
and the log line can say WHY without ever printing the key or the body.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time

HEADER = "x-retell-signature"
MAX_SKEW_MS = 5 * 60 * 1000

REFUSE_MISSING = "the request carries no X-Retell-Signature header"
REFUSE_MALFORMED = "the X-Retell-Signature header is not in the form v=<ms>,d=<hex>"
REFUSE_STALE = "the signature's timestamp is more than five minutes from now"
REFUSE_MISMATCH = "the signature does not match the request body"
REFUSE_NO_KEY = "no Retell API key is configured to verify with"

_HEADER_RE = re.compile(r"^\s*v=(\d{10,16})\s*,\s*d=([0-9a-fA-F]{64})\s*$")


def parse(header: str | None) -> tuple[str, str] | None:
    """`(timestamp_ms, hex_digest)` from `v=<ms>,d=<hex>`, or None if malformed."""
    match = _HEADER_RE.match(str(header or ""))
    if not match:
        return None
    return match.group(1), match.group(2).lower()


def digest_for(key: str, raw_body: bytes, timestamp_ms: str) -> str:
    """The hex HMAC Retell sends for this body and timestamp."""
    message = bytes(raw_body) + str(timestamp_ms).encode("ascii")
    return hmac.new(str(key).encode("utf-8"), message, hashlib.sha256).hexdigest()


def header_for(key: str, raw_body: bytes, timestamp_ms: int | str) -> str:
    """A valid header for `raw_body` — what Retell would send. For tests and for operators
    replaying a request by hand; never used to decide anything."""
    ts = str(timestamp_ms)
    return f"v={ts},d={digest_for(key, raw_body, ts)}"


def verify(raw_body: bytes, header: str | None, key: str, *,
           now_ms: int | None = None) -> str | None:
    """None when the request is Retell's; otherwise the sentence saying why it is not."""
    if not key:
        return REFUSE_NO_KEY
    if not str(header or "").strip():
        return REFUSE_MISSING
    parsed = parse(header)
    if parsed is None:
        return REFUSE_MALFORMED
    ts, sent = parsed
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    if abs(now - int(ts)) > MAX_SKEW_MS:
        return REFUSE_STALE
    if not hmac.compare_digest(digest_for(key, raw_body, ts), sent):
        return REFUSE_MISMATCH
    return None

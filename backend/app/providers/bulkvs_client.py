"""BulkVS REST API client — read-only DID inventory pull for the number sync (Ticket 03).

There is NO BulkVS inventory webhook, so owned-number inventory is POLLED from
GET /tnRecord (HTTP Basic auth). Each record carries the TN and its `ReferenceID` (the
operator's user-note/label, one-way mirrored into Number.friendly_name) plus routing.

These REST creds (BULKVS_API_USERNAME/PASSWORD) are SEPARATE from the SIP trunk creds.
Parsing is split from the HTTP call so tests can feed a faked /tnRecord response with no
network — mirrors the reconciler's normalize-then-ingest split.
"""

from dataclasses import dataclass

import httpx

from app.core.config import settings

_TIMEOUT = 30.0


@dataclass
class BulkvsTn:
    """One owned DID as reported by /tnRecord, normalized for the sync."""

    phone_number: str          # E.164 (+1XXXXXXXXXX)
    reference_id: str | None   # BulkVS ReferenceID = the label we mirror to friendly_name


def _to_e164(tn: str) -> str:
    """Normalize a BulkVS TN to E.164. BulkVS reports bare NANP digits (10- or 11-digit,
    e.g. "9195551234" / "19195551234"); some responses already include a leading '+'. Any
    non-digit punctuation is stripped. Non-NANP-looking values are returned digits-only so
    the sync still keys on a stable string rather than silently dropping the DID."""
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


def parse_tn_records(data) -> list[BulkvsTn]:
    """Turn a decoded /tnRecord JSON body into normalized BulkvsTn rows (pure — no HTTP).

    Tolerant of shape: BulkVS returns a JSON array of records, but some deployments wrap it
    in an object (e.g. {"TNs": [...]}). Records with no recognizable TN field are skipped.
    Field names are matched case-insensitively across the known aliases (TN / Number)."""
    if isinstance(data, dict):
        records = data.get("TNs") or data.get("tnRecords") or data.get("records") or []
    else:
        records = data or []

    out: list[BulkvsTn] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        tn = rec.get("TN") or rec.get("Number") or rec.get("tn") or rec.get("number")
        if not tn:
            continue
        ref = rec.get("ReferenceID")
        if ref is None:
            ref = rec.get("Reference") or rec.get("referenceId")
        ref = (str(ref).strip() or None) if ref is not None else None
        out.append(BulkvsTn(phone_number=_to_e164(str(tn)), reference_id=ref))
    return out


async def fetch_tn_records() -> list[BulkvsTn]:
    """GET /tnRecord and return the owned DIDs, normalized. Best-effort at the call site:
    the worker wraps this and logs+skips on failure so one bad poll never crashes the loop."""
    url = f"{settings.BULKVS_API_BASE.rstrip('/')}/tnRecord"
    auth = (settings.BULKVS_API_USERNAME, settings.BULKVS_API_PASSWORD)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, auth=auth)
        resp.raise_for_status()
        return parse_tn_records(resp.json())

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
    # Carrier-reported provisioning status, verbatim (e.g. "Active", "SUBMITTED" for a
    # pending port-in). Only an Active DID is operable — the sync mirrors this into
    # Number.provider_status and every operation gate refuses non-Active DIDs.
    status: str | None = None

    # --- "TN Details" block, mirrored for cost estimation (Billing tab) -------------------
    # `tier` selects the inbound per-minute rate and is the most cost-sensitive field BulkVS
    # reports: the published tiers span $0.0003 to $0.0198/min. Syncing it means per-number
    # rating is never hand-maintained and self-corrects if BulkVS re-tiers a DID.
    tier: str | None = None
    state: str | None = None
    rate_center: str | None = None
    # Carrier-side CNAM delivery. When true BulkVS performs (and bills) a lookup per inbound
    # call — at short call durations that per-event charge can exceed the minutes themselves.
    cnam: bool = False
    # Raw "YYYY-MM-DD HH:MM:SS" as reported; parsed by the sync.
    activation_date: str | None = None


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
        st = rec.get("Status") or rec.get("status")
        st = (str(st).strip() or None) if st is not None else None

        # "TN Details" carries the tier/rate-center/CNAM/activation block. Absent on older
        # responses (and on non-BulkVS-shaped fakes in tests), so every field is optional and
        # a missing block simply leaves them None — a DID with no tier is later flagged
        # `unrated` rather than being priced at a guessed rate.
        details = rec.get("TN Details") or rec.get("TNDetails") or {}
        if not isinstance(details, dict):
            details = {}

        def _s(key: str) -> str | None:
            v = details.get(key)
            return (str(v).strip() or None) if v is not None else None

        out.append(
            BulkvsTn(
                phone_number=_to_e164(str(tn)),
                reference_id=ref,
                status=st,
                tier=_s("Tier"),
                state=_s("State"),
                rate_center=_s("Rate Center"),
                cnam=bool(details.get("Cnam")),
                activation_date=_s("Activation Date"),
            )
        )
    return out


async def fetch_tn_records() -> list[BulkvsTn]:
    """GET /tnRecord and return the owned DIDs, normalized. Best-effort at the call site:
    the worker wraps this and logs+skips on failure so one bad poll never crashes the loop.

    /tnRecord has no offset/cursor pagination and no total-count in its response — just an
    optional `Limit` query param (undocumented default when omitted). Pass an explicit high
    Limit so the sync never silently relies on that default and truncates the DID list
    (which would soft-release real numbers past the cap on the next poll)."""
    url = f"{settings.BULKVS_API_BASE.rstrip('/')}/tnRecord"
    auth = (settings.BULKVS_API_USERNAME, settings.BULKVS_API_PASSWORD)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params={"Limit": "10000"}, auth=auth)
        resp.raise_for_status()
        return parse_tn_records(resp.json())


async def fetch_ported_numbers() -> set[str]:
    """Every DID that arrived by PORT, as E.164, from `/portTn`.

    Billing needs this because acquisition cost differs by route: a US port-in is FREE, while
    buying a number from inventory costs a $0.05 setup fee. Without it the Billing tab charges
    setup on every number and overstates one-time costs.

    Two calls: `/portTn` lists orders, then each order is fetched for its TN list (the list
    endpoint returns only OrderId/status, not the numbers). Any single order that fails to
    load is skipped rather than losing the whole set — a partial answer is better than none,
    and the sync re-runs on a schedule.
    """
    base = f"{settings.BULKVS_API_BASE.rstrip('/')}/portTn"
    auth = (settings.BULKVS_API_USERNAME, settings.BULKVS_API_PASSWORD)
    ported: set[str] = set()

    def _body(resp):
        """Parse a /portTn response. This endpoint answers **HTTP 300 Multiple Choices** with
        a perfectly good JSON body — `raise_for_status()` rejects that, so only 4xx/5xx are
        treated as failures here."""
        if resp.status_code >= 400:
            resp.raise_for_status()
        return resp.json()

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        orders = _body(await client.get(base, auth=auth))
        if not isinstance(orders, list):
            return ported
        for order in orders:
            if not isinstance(order, dict):
                continue
            oid = order.get("OrderId") or order.get("OrderID")
            if not oid:
                continue
            try:
                body = _body(await client.get(base, params={"OrderId": str(oid)}, auth=auth))
            except Exception:  # noqa: BLE001 - skip this order, keep the rest
                continue
            if not isinstance(body, dict):
                continue
            for tn in body.get("TN List") or []:
                if isinstance(tn, dict) and tn.get("TN"):
                    ported.add(_to_e164(str(tn["TN"])))
    return ported


async def fetch_voice_cdr(start_epoch: int, end_epoch: int, call_type: str = "all") -> list[dict]:
    """GET /voice — BulkVS's OWN RATED call detail records.

    This is the authoritative billing feed: each record carries `perMinute` (the rate BulkVS
    actually applied) and `amount` (what they actually charged), so cost never has to be
    estimated from a local rate table. Verified against the live account — every record's
    implied billed seconds matches a 6-second increment, and a flow-forwarded call correctly
    appears as TWO records (one inbound, one outbound).

    `Type` must be one of e911 / inbound / 8xx / outbound / 8yy / all (the API returns
    {"Status": "Invalid Type"} otherwise). Start/End are unix epoch seconds.

    Returns the raw record dicts; normalization lives in the pure kernel
    (app.services.billing.parse_voice_record) so it is unit-testable without the network.
    Raises on non-2xx so the caller can log and skip — a bad poll must never lose data, and
    the scan is idempotent on the BulkVS callID.
    """
    url = f"{settings.BULKVS_API_BASE.rstrip('/')}/voice"
    auth = (settings.BULKVS_API_USERNAME, settings.BULKVS_API_PASSWORD)
    params = {"Type": call_type, "Start": str(int(start_epoch)), "End": str(int(end_epoch))}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params=params, auth=auth)
        resp.raise_for_status()
        data = resp.json()
    if isinstance(data, dict):
        # An error body comes back as an object (e.g. {"Status": "Invalid Type", ...}) with
        # HTTP 200, so a 2xx alone is not success — surface it rather than silently costing 0.
        raise ValueError(f"BulkVS /voice returned an error body: {data!r}")
    return [r for r in (data or []) if isinstance(r, dict)]


_REF_KEYS = ("RefId", "RefID", "refId", "refid", "MessageRef", "MessageId", "MessageID",
             "Id", "id")


def _extract_ref_id(data, _depth: int = 0) -> str | None:
    """Pull the BulkVS message reference id out of a /messageSend response.

    WIDENED 2026-09-16, because the first REAL send — the day texting went live — came back
    with `ref=None` in the worker log and left the CRM's bubble on "queued" for ever, since
    the delivery-status webhook matches on `bulkvs-<RefId>` and there was no RefId to match.

    The old version looked only at the TOP LEVEL of a dict. BulkVS's own documentation shows
    `/messageSend` answering with a per-recipient `Results` list, so a response shaped
    `{"Results": [{"To": "...", "Status": "SUCCESS", "RefId": "..."}]}` — or a bare list of
    those — has an id this could not see. It now walks dicts and lists to a bounded depth
    and takes the first id it finds.

    **What BulkVS actually returns on this account is still not known**, and this function
    is not where that gets settled: it cannot be, because it only sees what was there. So
    `send_result` below keeps the WHOLE decoded body, `handle_message_send` stores it on the
    message row, and the next real send writes the answer down where a person can read it.
    Until then the CRM's bubble is advanced from OWEN's own knowledge that the send
    succeeded, which does not depend on an id at all.
    """
    if _depth > 4:
        return None
    if isinstance(data, dict):
        for k in _REF_KEYS:
            v = data.get(k)
            # A nested object under one of these names is not an id; keep walking.
            if v and not isinstance(v, (dict, list, tuple)):
                return str(v)
        for v in data.values():
            if isinstance(v, (dict, list, tuple)):
                found = _extract_ref_id(v, _depth + 1)
                if found:
                    return found
        return None
    if isinstance(data, (list, tuple)):
        for v in data:
            found = _extract_ref_id(v, _depth + 1)
            if found:
                return found
    return None


@dataclass
class SendResult:
    """What one `/messageSend` actually did, including the body it answered with.

    The body is kept because the ref id was missing on the first live send and nobody could
    say what BulkVS had really returned — the log line printed `ref=None` and threw the
    evidence away. `handle_message_send` writes this onto `messages.raw_payload`, so the
    question is answered by the next real text instead of by another guess.
    """

    ref_id: str | None
    status_code: int
    body: object = None


async def send_message(
    from_number: str, to_number: str, body: str, media_urls: list[str] | None = None
) -> str | None:
    """`send_result`, keeping only the ref id. The shape every caller before 2026-09-16
    used; left in place so nothing that only wants the id has to change."""
    return (await send_result(from_number, to_number, body, media_urls)).ref_id


async def send_result(
    from_number: str, to_number: str, body: str, media_urls: list[str] | None = None
) -> SendResult:
    """POST /messageSend to originate an outbound SMS/MMS from a 10DLC-registered DID and
    return what it answered. Raises on non-2xx so the worker retries with backoff.

    UPDATED 2026-09-16, the day texting went live. The request shape IS now exercised: a real
    text went out and BulkVS answered 2xx, so `From` = the DID, `To` = an array and `Message`
    = the body are right. What is still NOT known is the RESPONSE shape — `_extract_ref_id`
    found nothing in it, which is why the whole body is returned now rather than discarded,
    and why the CRM no longer depends on a RefId to know a text was sent.

    `MediaURLs` makes it an MMS. The URLs are OWEN's own signed, short-lived, single-object
    links (`integrations/crm/media.py`) — never a third party's. The field name follows the
    same BulkVS docs as the rest and, like the rest, is confirmed by a send or not at all.
    HTTP Basic auth reuses the REST creds like /tnRecord."""
    url = f"{settings.BULKVS_API_BASE.rstrip('/')}/messageSend"
    auth = (settings.BULKVS_API_USERNAME, settings.BULKVS_API_PASSWORD)
    payload: dict = {"From": from_number, "To": [to_number], "Message": body}
    if media_urls:
        payload["MediaURLs"] = list(media_urls)
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, json=payload, auth=auth)
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 - a 2xx with a non-JSON body still counts as sent
            # The text is recorded verbatim, truncated: an unparseable answer is still the
            # evidence of what this endpoint does, and it was being discarded.
            return SendResult(None, resp.status_code, {"_text": resp.text[:2000]})
        return SendResult(_extract_ref_id(data), resp.status_code, data)

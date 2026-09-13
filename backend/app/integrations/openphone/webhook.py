"""Quo (OpenPhone) webhooks: verify, record, enqueue, answer — and process later.

The owner, 2026-09-13: "lets do the quo webhook i will wire it up just live it all ready
in the correct way then you give me the url". The poll in `sync.py` stays on as the
backstop; this makes new activity arrive in seconds instead of within five minutes.

## What changed since `sync.py` said "polling, not webhooks"

That decision rested on two facts, and the owner removed both: registering a webhook is a
WRITE (`POST /v1/webhooks`), which OWEN may not make — so the OWNER registers it by hand in
the Quo dashboard; and the signing secret comes back only from that call — so the owner
copies it from the dashboard ("Reveal signing secret") into `OPENPHONE_WEBHOOK_SECRET`.
**OWEN still issues no non-GET request to OpenPhone.** There is no registration code here,
and the mirror's no-write test drives this module too.

## The public route, and the order of its guards

    POST https://api.${APP_DOMAIN}/webhooks/openphone

  1. `OPENPHONE_WEBHOOK_ENABLED` false  -> 404, before the body is read. Nothing else runs.
  2. no `OPENPHONE_WEBHOOK_SECRET`      -> 503. Nothing recorded; Quo retries later.
  3. signature missing / malformed / wrong / outside the 5-minute window -> 401. Nothing
     recorded, nothing enqueued, and the reason is logged WITHOUT the secret or the body.
  4. an event type we do not mirror     -> 200 `{"ignored": ...}`. Acknowledged, so Quo
     does not retry it forever; nothing recorded.
  5. the mirror itself refuses (`OPENPHONE_MIRROR_ENABLED`, `OPENPHONE_API_KEY`,
     `AGENT_RUNTIME_KEY`)               -> 503. Nothing recorded; Quo retries later.
  6. otherwise: ONE `crm_report` job is written (that row is the record) and the route
     answers 200. No OpenPhone request and no CRM request happens on this path, so it
     answers in the time one INSERT takes.

## Processing reuses the mirror, so a webhook and the poll make ONE CRM event

The job posts back into OWEN's own `POST /api/openphone-mirror/webhook-events` (the same
worker-to-app hop every mirrored object already takes), which calls `process()` below.
That hands the object to `sync._mirror_message` / `sync._mirror_call` — the exact functions
the poll uses — so `openphone_mirror_rows UNIQUE (kind, external_id)` stops a second
enqueue, and the CRM's UNIQUE `dedupe_key` (`openphone:<kind>:<id>`) stops a second row.
Webhook-then-poll, poll-then-webhook and a Quo retry all land on one event.

A call is completed before its recording, transcript and summary exist, and Quo announces
each separately. For a call already mirrored, each is sent ONCE (its own state row,
`call_recording` / `call_transcript` / `call_summary`) under the call's own dedupe key; the
CRM fills in what the first delivery could not carry and overwrites nothing.

## THE SIGNATURE — the scheme, and how sure we are of it

From Quo's own published documentation ("Webhooks", support.quo.com, fetched 2026-09-13):

    openphone-signature: hmac;1;<timestamp>;<base64 digest>
    signed data = <timestamp> + "." + <payload>
    key         = base64-decode(<signing secret>)
    digest      = base64( HMAC-SHA256(key, signed data) )

VERIFIED against that documentation, including both of its code samples. Two details the
samples leave open, and what this does about each:

  * **Which bytes are "the payload".** The Python sample signs the RAW request body; the
    Node sample signs `JSON.stringify(req.body)`, i.e. the compact re-serialisation. Those
    are the same bytes whenever Quo sends compact JSON, which is what both samples imply.
    Both candidates are computed and compared in constant time; either matching is
    accepted. Neither can be produced without the key.
  * **The timestamp's unit.** The documented example is `1639710054089` — milliseconds.
    It is read as milliseconds.

NOT verified against a live delivery: no webhook has ever reached this code, because none
can until the owner registers it. The first real delivery is the test.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.core.config import settings
from app.integrations.openphone import config as op_config
from app.integrations.openphone import push

logger = logging.getLogger("integrations.openphone.webhook")

WEBHOOK_PATH = "/webhooks/openphone"
PROCESS_PATH = "/api/openphone-mirror/webhook-events"
SIGNATURE_HEADER = "openphone-signature"

# The events the owner ticks in the Quo dashboard. Anything else is acknowledged and
# ignored — `call.ringing`, `contact.*`, `task.*` — because a 4xx would make Quo retry it
# and eventually disable the webhook.
MESSAGE_EVENTS = frozenset({"message.received", "message.delivered"})
CALL_EVENTS = frozenset({"call.completed"})
CALL_PART_EVENTS = {
    # event type -> the state-row kind that records "this part was sent once"
    "call.recording.completed": "call_recording",
    "call.transcript.completed": "call_transcript",
    "call.summary.completed": "call_summary",
}
HANDLED_EVENTS = MESSAGE_EVENTS | CALL_EVENTS | frozenset(CALL_PART_EVENTS)

REFUSE_DISABLED = "the Quo webhook is disabled (OPENPHONE_WEBHOOK_ENABLED=false)"
REFUSE_NO_SECRET = "no signing secret is configured (OPENPHONE_WEBHOOK_SECRET)"


# --- the signature (pure) ---------------------------------------------------------------


def parse_signature(header: Optional[str]) -> Optional[tuple[str, str]]:
    """`(timestamp, digest)` from `hmac;1;<timestamp>;<digest>`, or None if malformed."""
    fields = str(header or "").strip().split(";")
    if len(fields) != 4:
        return None
    scheme, version, timestamp, digest = (f.strip() for f in fields)
    if scheme != "hmac" or version != "1" or not timestamp.isdigit() or not digest:
        return None
    return timestamp, digest


def _signing_key(secret: str) -> Optional[bytes]:
    try:
        return base64.b64decode(str(secret or "").strip(), validate=True) or None
    except (binascii.Error, ValueError):
        return None


def digest_for(key: bytes, timestamp: str, payload: bytes) -> str:
    signed = timestamp.encode("ascii") + b"." + payload
    return base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")


def _payload_candidates(raw: bytes) -> list[bytes]:
    """The raw body, plus its compact re-serialisation (the Node sample's
    `JSON.stringify(req.body)`). See the module docstring."""
    out = [raw]
    try:
        compact = json.dumps(json.loads(raw), separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return out
    if compact != raw:
        out.append(compact)
    return out


def signature_refusal(header: Optional[str], raw: bytes, secret: str, *,
                      now_ms: Optional[int] = None,
                      tolerance_seconds: int = 300) -> Optional[str]:
    """Why this delivery is NOT authentic, or None if it is. Never raises."""
    key = _signing_key(secret)
    if key is None:
        return "the configured signing secret is not valid base64"
    if not str(header or "").strip():
        return "missing %s header" % SIGNATURE_HEADER
    parsed = parse_signature(header)
    if parsed is None:
        return "malformed %s header" % SIGNATURE_HEADER
    timestamp, provided = parsed
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    if abs(now_ms - int(timestamp)) > int(tolerance_seconds) * 1000:
        return "signature timestamp is outside the %ds window" % int(tolerance_seconds)
    # Every candidate is computed and compared, so the time taken does not depend on
    # which one matched.
    matched = False
    for payload in _payload_candidates(raw):
        if hmac.compare_digest(digest_for(key, timestamp, payload).encode("ascii"),
                               provided.encode("ascii", "replace")):
            matched = True
    return None if matched else "signature does not match"


# --- the fast path ----------------------------------------------------------------------


def _process_url() -> str:
    return f"{settings.OWEN_INTERNAL_URL.rstrip('/')}{PROCESS_PATH}"


async def _enqueue(job_body: dict) -> None:
    from app.db import SessionLocal
    from app.services import queue

    async with SessionLocal() as db:
        await queue.enqueue(db, push.JOB_TYPE, {
            "url": _process_url(),
            "headers": {"X-OWEN-Key": settings.AGENT_RUNTIME_KEY},
            "body": job_body,
        })


async def accept(raw: bytes, header: Optional[str], *, now_ms: Optional[int] = None,
                 enqueue=None) -> tuple[int, dict]:
    """Everything the route does after the kill switch. `(status, body)`.

    `enqueue` is injectable so the tests can prove what was — and was not — written.
    """
    enqueue = enqueue or _enqueue
    secret = settings.OPENPHONE_WEBHOOK_SECRET
    if not str(secret or "").strip():
        logger.warning("quo-webhook: refused a delivery — %s", REFUSE_NO_SECRET)
        return 503, {"ok": False, "error": REFUSE_NO_SECRET}

    why = signature_refusal(header, raw, secret, now_ms=now_ms,
                            tolerance_seconds=settings.OPENPHONE_WEBHOOK_TOLERANCE_SECONDS)
    if why:
        # The reason, never the secret, the header or the body.
        logger.warning("quo-webhook: REFUSED a delivery (%s)", why)
        return 401, {"ok": False, "error": "invalid signature"}

    try:
        event = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return 400, {"ok": False, "error": "body is not JSON"}
    if not isinstance(event, dict):
        return 400, {"ok": False, "error": "body is not a JSON object"}

    kind = str(event.get("type") or "")
    if kind not in HANDLED_EVENTS:
        logger.info("quo-webhook: ignoring %s event %s", kind or "untyped", event.get("id"))
        return 200, {"ok": True, "ignored": kind}

    stop = push.refusal()
    if stop:
        logger.warning("quo-webhook: not accepting %s — %s", kind, stop)
        return 503, {"ok": False, "error": "the Quo mirror is not ready"}

    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    obj = data.get("object") if isinstance(data.get("object"), dict) else {}
    await enqueue({
        "source": "webhook",
        "event_id": str(event.get("id") or ""),
        "type": kind,
        "created_at": event.get("createdAt"),
        "object": obj,
    })
    logger.info("quo-webhook: queued %s event %s", kind, event.get("id"))
    return 200, {"ok": True, "queued": kind}


router = APIRouter(tags=["openphone-webhook"])


@router.post(WEBHOOK_PATH)
async def receive(request: Request) -> Response:
    """Quo's webhook. See the module docstring for the order of the guards."""
    if not settings.OPENPHONE_WEBHOOK_ENABLED:
        return Response(status_code=404)
    raw = await request.body()
    status_code, body = await accept(raw, request.headers.get(SIGNATURE_HEADER))
    return JSONResponse(body, status_code=status_code)


# --- processing (runs in the app, reached by the worker's crm_report job) ---------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ts(value) -> Optional[datetime]:
    from app.integrations.openphone.sync import _parse_dt

    return _parse_dt(value)


def _parties(obj: dict) -> list[str]:
    out: list[str] = []
    for field in ("from", "to"):
        value = obj.get(field)
        for item in (value if isinstance(value, list) else [value]):
            if isinstance(item, str) and item:
                out.append(item)
    for item in obj.get("participants") or []:
        if isinstance(item, str) and item:
            out.append(item)
    return out


def _our_line(obj: dict, lines: dict[str, str]) -> Optional[str]:
    """The mirrored OpenPhone line this object belongs to, or None."""
    line = lines.get(str(obj.get("phoneNumberId") or ""))
    if line:
        return line
    keys = {op_config.match_key(n): n for n in lines.values()}
    for party in _parties(obj):
        if op_config.match_key(party) in keys:
            return keys[op_config.match_key(party)]
    return None


def _customer(obj: dict, line_key: str) -> str:
    for party in _parties(obj):
        key = op_config.match_key(party)
        if key and key != line_key:
            return party
    return ""


def call_entry(obj: dict) -> dict:
    """A webhook call object in the shape `sync._mirror_call` reads.

    The webhook shape carries no `duration`, and says `status: "completed"` for a call
    that went to voicemail and was never answered (Quo's own example). Both are derived
    here, from the timestamps and the voicemail, rather than guessed downstream.
    """
    entry = dict(obj)
    answered, completed = _ts(obj.get("answeredAt")), _ts(obj.get("completedAt"))
    if entry.get("duration") is None and completed is not None:
        entry["duration"] = (max(0, int((completed - answered).total_seconds()))
                             if answered is not None else 0)
    if answered is None and str(obj.get("status") or "").lower() == "completed":
        entry["status"] = "voicemail" if obj.get("voicemail") else "no-answer"
    if obj.get("media") or obj.get("voicemail"):
        entry["hasRecording"] = True
    return entry


def _transcript_text(obj: dict) -> str:
    lines = []
    for seg in obj.get("dialogue") or []:
        if not isinstance(seg, dict):
            continue
        who = str(seg.get("identifier") or "").strip()
        said = str(seg.get("content") or "").strip()
        if said:
            lines.append(f"{who}: {said}" if who else said)
    return "\n".join(lines)


def _summary_text(obj: dict) -> str:
    summary = obj.get("summary")
    if isinstance(summary, list):
        return " ".join(str(s).strip() for s in summary if str(s).strip())
    return str(summary or "").strip()


async def _fetch_call(op, call_id: str) -> dict:
    """`GET /calls/{id}` — a part event (transcript, summary) carries only the id."""
    call = await op.get_call(call_id)
    if not isinstance(call, dict) or not call.get("id"):
        raise RuntimeError("OpenPhone returned no call for that id")
    return call


async def process(body: dict, *, session_factory=None) -> dict:
    """Mirror one webhook object through the poll's own idempotent functions.

    Raises on an OpenPhone read failure so the route answers 502 and the queue retries.
    """
    from app.db import SessionLocal
    from app.integrations.openphone import sync
    from app.integrations.openphone.events import MirroredCall
    from app.providers import openphone_client as op

    cfg = op_config.current()
    kind = str((body or {}).get("type") or "")
    obj = (body or {}).get("object") if isinstance((body or {}).get("object"), dict) else {}
    if kind not in HANDLED_EVENTS:
        return {"ok": True, "ignored": kind}

    numbers = await op.list_phone_numbers()
    lines = {str(n.get("id") or ""): str(n.get("number") or "")
             for n in numbers if isinstance(n, dict) and cfg.mirrors(n.get("number"))}

    factory = session_factory or SessionLocal
    async with factory() as db:
        if kind in MESSAGE_EVENTS:
            line = _our_line(obj, lines)
            if not line:
                return {"ok": True, "skipped": "not a mirrored line"}
            key = op_config.match_key(line)
            entry = dict(obj)
            entry.setdefault("text", obj.get("body") or obj.get("text") or "")
            outcome = await sync._mirror_message(
                db, entry, line_number=line, line_key=key,
                participant=_customer(obj, key), since=_EPOCH, dry_run=False)
            return {"ok": True, "type": kind, "outcome": outcome}

        call_id = str(obj.get("id") if kind in CALL_EVENTS
                      or kind == "call.recording.completed" else obj.get("callId") or "")
        if not call_id:
            return {"ok": True, "skipped": "no call id"}
        call = obj if kind in CALL_EVENTS or kind == "call.recording.completed" else None
        mirrored = await sync._already_mirrored(db, "call", call_id)

        if kind in CALL_EVENTS or not mirrored:
            call = call if call is not None else await _fetch_call(op, call_id)
            line = _our_line(call, lines)
            if not line:
                return {"ok": True, "skipped": "not a mirrored line"}
            key = op_config.match_key(line)
            outcome = await sync._mirror_call(
                db, call_entry(call), line_number=line, line_key=key,
                participant=_customer(call, key), since=_EPOCH, cfg=cfg, dry_run=False)
            return {"ok": True, "type": kind, "outcome": outcome}

        # A later PART of a call already on the thread. Sent once, under the call's own
        # dedupe key, so the CRM fills in the blank rather than writing a second row.
        part = CALL_PART_EVENTS[kind]
        if await sync._already_mirrored(db, part, call_id):
            return {"ok": True, "type": kind, "outcome": "duplicate"}
        call = call if call is not None else await _fetch_call(op, call_id)
        line = _our_line(call, lines)
        if not line:
            return {"ok": True, "skipped": "not a mirrored line"}
        key = op_config.match_key(line)
        entry = call_entry(call)
        occurred = _ts(entry.get("createdAt") or entry.get("completedAt"))
        facts = MirroredCall(
            external_id=call_id,
            customer_number=_customer(call, key),
            line_number=line,
            direction=str(entry.get("direction") or "incoming"),
            status=str(entry.get("status") or ""),
            duration_seconds=entry.get("duration"),
            occurred_at=occurred.isoformat() if occurred else None,
            has_recording=kind == "call.recording.completed" or bool(entry.get("hasRecording")),
            transcript=_transcript_text(obj) if kind == "call.transcript.completed" else "",
            summary=_summary_text(obj) if kind == "call.summary.completed" else "",
        )
        outcome = await sync._record_and_enqueue(
            db, kind=part, external_id=call_id, customer_number=facts.customer_number,
            line_number=line, occurred_at=occurred, payload=facts.as_payload())
        return {"ok": True, "type": kind, "outcome": outcome}


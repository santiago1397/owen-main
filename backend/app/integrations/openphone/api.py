"""`/api/openphone-mirror/*` — the delivery hop, and the recording stream.

Two routes, and they face opposite directions.

  POST /api/openphone-mirror/events          <- OWEN's own WORKER, draining a `crm_report`
                                                job. Resolves the contact and posts the
                                                event to the CRM. Scope: `agent_write`,
                                                exactly as the CRM link's three adapters.

  GET  /api/openphone-mirror/recordings/{id} <- the CRM's backend, on behalf of a browser.
                                                Streams the audio. Scope: `crm_link`.

## Why this is a router of its own and not four more `/api/crm-link` routes

`tests/test_crm_softphone_creds.py` fences that router at exactly the seven routes it has,
and the fence has already earned its keep once — it caught a merge that changed the public
surface before a deploy did. Widening it for an unrelated integration would spend that
guarantee on convenience. A separate prefix keeps the CRM link's surface pinned, and this
module gets its own fence (`tests/test_openphone_mirror.py`) rather than inheriting one.

## Why the recording route reuses `crm_link` instead of minting a scope

The CRM already holds a key with `crm_link`, which authorises it to PLACE A CALL and SEND
AN SMS from a bound DID. Streaming a read-only recording is strictly less than that key can
already do, so a new scope would mean provisioning and rotating a second credential to gain
nothing. A scope claiming to be read-only while permitting writes would be a falsehood a
machine consumer reads; a write-capable scope also permitting a read is not.

## The key never gets near the browser, and neither does the media URL

Three hops, each one dropping a credential:

    browser  --cookie-->  CRM  --X-OWEN-Key-->  OWEN  --OpenPhone key-->  api.openphone.com
                                                     --no credential--->  share.quo.com

The browser's `<audio src="/api/openphone/recordings/AC123">` is a CRM-relative path, so it
carries the operator's CRM session cookie and nothing else. The CRM proxies to here with its
OWEN key. OWEN asks OpenPhone for the media URL with the OpenPhone key, then fetches that
URL with NO credential at all (it is a share link that carries its own).

The bytes are streamed rather than the URL redirected. A redirect would be simpler and would
still not leak the API key — but it would hand the browser a share.quo.com URL that is
forwardable and outlives the session, and the owner's instruction was that recordings stream
through OWEN. Note the tradeoff this creates, recorded in the CRM's DECISIONS.md: cancelling
OpenPhone breaks this audio, because nothing is copied.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.api.ai.deps import require_scope
from app.core.apikeys import SCOPE_AGENT_WRITE, SCOPE_CRM_LINK
from app.integrations.crm import config as crm_config
from app.integrations.crm.client import CrmClient
from app.integrations.openphone import config as op_config
from app.integrations.openphone import sync as op_sync
from app.integrations.openphone.events import to_crm_event
from app.providers import openphone_client as op

logger = logging.getLogger("integrations.openphone.api")

router = APIRouter(prefix="/api/openphone-mirror", tags=["openphone-mirror"])


def _require_enabled() -> op_config.MirrorSettings:
    """503 while the kill switch is off. Checked before anything else on every route, so a
    disabled mirror never opens a session, never calls OpenPhone and never logs a customer's
    phone number."""
    cfg = op_config.current()
    if not cfg.enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the OpenPhone mirror is disabled "
            "(set OPENPHONE_MIRROR_ENABLED=true to enable it)",
        )
    return cfg


class MirrorDeliveryIn(BaseModel):
    """The `crm_report` job body, i.e. `MirroredCall.as_payload()` / `MirroredMessage`.

    `extra="allow"` for the reason the CRM link's equivalents give: a job enqueued by an
    older deploy has to drain against a newer one without 422-ing in a retry loop.
    """

    model_config = {"extra": "allow"}

    kind: str = ""
    external_id: str = ""
    customer_number: str = ""
    line_number: str = ""
    direction: str = "incoming"
    occurred_at: str | None = None
    extra: dict = Field(default_factory=dict)


@router.post("/events")
async def deliver_mirrored(
    body: MirrorDeliveryIn,
    _key=Depends(require_scope(SCOPE_AGENT_WRITE)),
) -> dict:
    """Deliver one mirrored OpenPhone object to the CRM.

    The status code is the retry contract `workers/handlers.py::handle_crm_report` reads,
    and it is the CRM link's, unchanged:

      * **200** — delivered, or permanently undeliverable. The job completes.
      * **502** — the CRM was unreachable or answered 5xx. The queue retries with backoff.
      * **200 with `ok: false`** — a CRM 4xx. The payload is wrong; five more attempts
        produce the same 4xx. Logged at WARNING, so it surfaces in `/api/ai/errors`.

    A 502 retry is exactly the window the CRM's `dedupe_key` column exists to close: the CRM
    may have committed the event before the response was lost, and the retry must land on
    the same row rather than a second one.
    """
    _require_enabled()
    payload = body.model_dump()

    cfg = crm_config.current()
    # The mirror rides the CRM link's own destination and credential. It is the same CRM,
    # and a second base URL for one integration would be a second thing to rotate.
    base_url, token = cfg.base_url, cfg.token
    if not token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            crm_config.REFUSE_NO_TOKEN)

    client = CrmClient(base_url, token, timeout_s=cfg.http_timeout_seconds,
                       budget_s=cfg.http_budget_seconds)
    customer = str(payload.get("customer_number") or "")
    contact_id, reason = await client.resolve_contact_id(customer)
    if contact_id is None:
        # Not a drop. The body carries `from_number` and the CRM matches on the last ten
        # digits or CREATES the contact — the same path the BulkVS stranger takes, which is
        # the owner's requirement 5 ("an unknown caller auto-creates a contact").
        logger.info("openphone-mirror: no contact_id for %s %s (%s) — sending from_number "
                    "for the CRM to match or create", payload.get("kind"),
                    payload.get("external_id"), reason)

    try:
        crm_body = to_crm_event(payload, contact_id)
    except ValueError as exc:
        # An unknown `kind`. Our bug, and not retryable — do not spin the queue on it.
        logger.error("openphone-mirror: refusing to deliver an unknown payload: %s", exc)
        return {"ok": False, "reason": str(exc)}

    result = await client.post_event(crm_body)
    if result.ok:
        return {"ok": True, "kind": payload.get("kind"),
                "external_id": payload.get("external_id"),
                "contact_id": contact_id, "crm_status": result.status}
    if result.retryable:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"CRM did not accept the mirrored event ({result.status}): {result.reason}",
        )
    return {"ok": False, "reason": f"CRM {result.status}: {result.reason}",
            "kind": payload.get("kind")}


@router.get("/recordings/{call_id}")
async def stream_recording(
    call_id: str,
    _key=Depends(require_scope(SCOPE_CRM_LINK)),
) -> Response:
    """Stream one OpenPhone call recording. GET only, and read-only all the way down.

    404 when OpenPhone has no recording for the call — which is the ordinary case for a
    missed call, not an error. 502 when OpenPhone itself is unreachable, so the CRM can tell
    "there is no audio" from "we could not reach the system that has it": the first is
    permanent and the second is worth retrying.
    """
    _require_enabled()
    try:
        meta = await op.get_call_recording(call_id)
    except Exception as exc:  # noqa: BLE001 - a 401/404/outage must not 500 the CRM
        logger.warning("openphone-mirror: recording lookup failed for %s (%s)",
                       call_id, type(exc).__name__)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "OpenPhone could not be reached for that recording") from None

    url = str((meta or {}).get("url") or "").strip()
    if not url:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "OpenPhone has no recording for that call")

    try:
        audio, content_type = await op.fetch_recording_bytes(url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("openphone-mirror: recording fetch failed for %s (%s)",
                       call_id, type(exc).__name__)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "the recording could not be fetched") from None

    # `private` because this is one customer's call audio and it must not sit in a shared
    # cache. The short max-age lets a browser scrub back and forth in the player without
    # re-fetching the whole file through three hops.
    return Response(content=audio, media_type=content_type or "audio/mpeg",
                    headers={"Cache-Control": "private, max-age=300"})


@router.get("/status")
async def mirror_status(_key=Depends(require_scope(SCOPE_CRM_LINK))) -> dict:
    """What the mirror is configured to do. Read-only, and names no customer.

    Exists so "is the mirror on, and is it seeing the line?" is answerable without shell
    access to the box — the question an operator asks first when a thread looks stale.
    """
    cfg = _require_enabled()
    return {
        "enabled": cfg.enabled,
        "api_key_present": cfg.api_key_present,
        "backfill_days": cfg.backfill_days,
        "poll_seconds": cfg.poll_seconds,
        "include": sorted(cfg.include),
        "exclude": sorted(cfg.exclude),
        "mode": "polling",
        "writes_to_openphone": False,
    }


@router.post("/preview")
async def preview(_key=Depends(require_scope(SCOPE_CRM_LINK))) -> dict:
    """Size the backfill without performing it. READS OpenPhone, writes NOTHING.

    A POST because it makes outbound requests and is rate-limit-relevant, not because it
    changes anything: `run_once(dry_run=True)` writes no state row, queues no job and never
    contacts the CRM. This is how the 30-day counts in `.qa/state/openphone-done` are meant
    to be obtained on a box that has the production key.
    """
    _require_enabled()
    return await op_sync.run_once(dry_run=True)

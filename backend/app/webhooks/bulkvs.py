"""BulkVS inbound SMS/MMS webhook — PUBLIC, verified by source-IP allow-list.

BulkVS MO (mobile-originated) messages arrive as unsigned JSON POSTs, so this surface can't
reuse the HMAC-signed build_router flow. It exposes ONLY /message (BulkVS has no status /
recording callbacks) and reuses the shared `verify_request` gate plus the existing inbound
messages ingest + GHL relay path UNCHANGED — BulkVS is just another provider feeding the
same upsert-on-SID `messages` table (Ticket 09).

Per-DID routing supports the same ?tracking_number= query override the other webhooks use.

CRM LINK (additive, opt-in, off by default). Both routes end with one call into
`integrations/crm/hook.py`, which does nothing unless `CRM_LINK_ENABLED` is true AND the DID
has an enabled `crm_links` row. The GoHighLevel relay above it is untouched and is enqueued
first; the CRM link is an addition to it and never a replacement.
"""

import logging

from fastapi import APIRouter, Request, Response
from sqlalchemy import select

from app.db import SessionLocal
from app.integrations.crm import hook as crm_hook
from app.models import Message
from app.providers.bulkvs import BULKVS_INBOUND_IPS, BulkvsAdapter
from app.services import queue, sms
from app.services.messages import apply_inbound_keyword, ingest_message_event
from app.webhooks.common import verify_request

logger = logging.getLogger("webhooks")

router = APIRouter(prefix="/webhooks/bulkvs", tags=["webhooks"])
_adapter = BulkvsAdapter()


@router.post("/message")
async def message(request: Request) -> Response:
    params = await verify_request(
        request, _adapter, "bulkvs", signature_headers=[], ip_allowlist=BULKVS_INBOUND_IPS
    )
    if params is None:
        return Response(status_code=403)

    # verify_request stringifies JSON values for signature parity; re-read the raw body so
    # the adapter sees native shapes (To may be an array, Attachments a list). Starlette
    # caches the parsed body, so this second read does no extra I/O.
    body = await request.json()
    if not isinstance(body, dict):
        body = {}
    tracking_number = request.query_params.get("tracking_number")
    if tracking_number:
        body["_tracking_number"] = tracking_number

    evt = _adapter.parse_message_event(body)
    logger.info("bulkvs message: sid=%s from=%s to=%s num_media=%s",
                evt.provider_message_sid, evt.from_number, evt.to_number, evt.num_media)
    async with SessionLocal() as db:
        # Asked BEFORE the upsert below, which cannot tell an insert from an update. Costs
        # nothing (and makes no query) while CRM_LINK_ENABLED is false. See crm/hook.py.
        crm_first_sight = await crm_hook.is_new_inbound_message(db, evt.provider_message_sid)
        msg = await ingest_message_event(db, "bulkvs", evt)
        # App-level opt-out: STOP/START/HELP maintain the per-(number, contact) opt-out state
        # (Ticket 10). The message itself is still stored + relayed — we only track consent.
        await apply_inbound_keyword(db, msg.number_id, evt.from_number, evt.body)
        await queue.enqueue(db, "message_relay_ghl", {"message_id": str(msg.id)})
        crm_message = {
            "message_id": str(msg.id),
            "from_number": msg.from_number or evt.from_number or "",
            "dialed_number": msg.to_number or evt.to_number or "",
            "body": msg.body or "",
            "num_media": int(msg.num_media or 0),
            "provider_message_sid": msg.provider_message_sid or "",
        }

    # ADDITIVE, opt-in, and deliberately AFTER everything above: the GoHighLevel relay is
    # already enqueued and committed by this point, so the CRM link cannot affect it. A DID
    # with no enabled `crm_links` row does nothing here, and neither does anything at all
    # while CRM_LINK_ENABLED is false. The hook is total — it returns False rather than
    # raising — so the 200 below is reached whatever the CRM is doing.
    if crm_first_sight:
        await crm_hook.handle_inbound_message(**crm_message)
    return Response(status_code=200)


@router.post("/message-status")
async def message_status(request: Request) -> Response:
    """BulkVS outbound delivery-status (DLR) callback — advances an OUTBOUND row's status
    forward-only (Ticket 10). Same IP-allowlist gate as /message (BulkVS DLRs are unsigned).
    Matches the row on the BulkVS RefId stamped into provider_message_sid at send time."""
    params = await verify_request(
        request, _adapter, "bulkvs", signature_headers=[], ip_allowlist=BULKVS_INBOUND_IPS
    )
    if params is None:
        return Response(status_code=403)

    body = await request.json()
    if not isinstance(body, dict):
        body = {}
    ref = (
        body.get("RefId") or body.get("RefID") or body.get("MessageRef")
        or body.get("MessageId") or body.get("MessageID") or body.get("Id")
    )
    new_status = body.get("Status") or body.get("status") or body.get("MessageStatus")
    if not ref:
        logger.warning("bulkvs message-status: no RefId in payload, ignoring")
        return Response(status_code=200)

    sid = f"bulkvs-{ref}"
    async with SessionLocal() as db:
        msg = (
            await db.execute(select(Message).where(Message.provider_message_sid == sid))
        ).scalar_one_or_none()
        if msg is None:
            logger.warning("bulkvs message-status: no message for ref=%s", ref)
            return Response(status_code=200)
        advanced = sms.advance_status(msg.status, new_status)
        if advanced != msg.status:
            logger.info("bulkvs message-status: %s %s -> %s", msg.id, msg.status, advanced)
            msg.status = advanced
            await db.commit()
    return Response(status_code=200)

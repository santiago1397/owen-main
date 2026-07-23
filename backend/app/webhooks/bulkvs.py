"""BulkVS inbound SMS/MMS webhook — PUBLIC, verified by source-IP allow-list.

BulkVS MO (mobile-originated) messages arrive as unsigned JSON POSTs, so this surface can't
reuse the HMAC-signed build_router flow. It exposes ONLY /message (BulkVS has no status /
recording callbacks) and reuses the shared `verify_request` gate plus the existing inbound
messages ingest + GHL relay path UNCHANGED — BulkVS is just another provider feeding the
same upsert-on-SID `messages` table (Ticket 09).

Per-DID routing supports the same ?tracking_number= query override the other webhooks use.
"""

import logging

from fastapi import APIRouter, Request, Response

from app.db import SessionLocal
from app.providers.bulkvs import BULKVS_INBOUND_IPS, BulkvsAdapter
from app.services import queue
from app.services.messages import ingest_message_event
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
        msg = await ingest_message_event(db, "bulkvs", evt)
        await queue.enqueue(db, "message_relay_ghl", {"message_id": str(msg.id)})
    return Response(status_code=200)

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
from app.providers import bulkvs as bulkvs_provider
from app.providers.bulkvs import BULKVS_INBOUND_IPS, BulkvsAdapter
from app.services import dlr, queue, sms
from app.services.dlr_junk import DLR_JUNK_KEY
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

    # A DELIVERY RECEIPT, NOT A MESSAGE (2026-09-16). BulkVS posts these to this same
    # webhook, and until this branch existed every one was stored as an inbound text and
    # relayed onward — so a customer's CRM thread showed two messages they never wrote.
    #
    # Checked FIRST, before the ingest upsert and before the CRM hook is even asked, so a
    # receipt cannot become a message by any path. The receipt is then used for what it is
    # actually for: advancing the outbound message it belongs to. See services/dlr.py.
    receipt = bulkvs_provider.parse_delivery_receipt(body)
    if receipt is not None:
        flag = bulkvs_provider.flagged_as_receipt(body)
        logger.info("bulkvs delivery receipt: id=%s stat=%s err=%s to=%s%s",
                    receipt.receipt_id, receipt.stat, receipt.err, receipt.customer_number,
                    (" (payload flagged it with %s)" % flag) if flag else "")
        await _take_receipt(receipt)
        return Response(status_code=200)
    if bulkvs_provider.looks_like_unparsed_receipt(body):
        # LOUD on purpose. A carrier variant we cannot read is how the first two junk
        # messages reached a customer's thread, and the only thing worse than not reading it
        # is not noticing. It still ingests below, exactly as it does today.
        logger.error("bulkvs: a message looks like a delivery receipt but could not be "
                     "parsed — it will be stored as an inbound text. Body shape: %r",
                     str(body.get("Message") or body.get("Body") or "")[:160])

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


async def _take_receipt(receipt) -> None:
    """Apply one receipt, and keep it either way.

    TOTAL, like every other hook on this webhook: any failure at all still returns the 200
    below, because a 500 here makes BulkVS re-deliver the receipt for ever and costs us
    nothing we did not already have.

    A receipt that correlates to nothing is STORED — as a message row, marked as receipt
    junk so it is hidden from every thread and never relayed — rather than dropped. Losing
    it would mean losing the only evidence of a failed text, and the marker is what makes
    the difference between "kept where somebody can look at it" and "in a customer's
    conversation".
    """
    try:
        async with SessionLocal() as db:
            outcome = await dlr.apply(db, receipt)
            if not outcome["applied"] and outcome["reason"] != "already applied":
                await _keep_orphan_receipt(db, receipt, outcome["reason"])
    except Exception:  # noqa: BLE001 - the webhook's 200 is worth more than this receipt
        logger.exception("bulkvs: applying delivery receipt %s failed", receipt.receipt_id)


async def _keep_orphan_receipt(db, receipt, why: str) -> None:
    """Store a receipt we could not place, marked so nothing ever shows or relays it."""
    evt = _adapter.parse_message_event(receipt.raw)
    msg = await ingest_message_event(db, "bulkvs", evt)
    raw = dict(msg.raw_payload or {})
    raw[DLR_JUNK_KEY] = {"id": receipt.receipt_id, "stat": receipt.stat,
                         "err": receipt.err, "uncorrelated": why}
    msg.raw_payload = raw
    # Never relayed to GoHighLevel and never to the CRM: it is not a message.
    msg.relayed_to_ghl = True
    await db.commit()
    logger.warning("bulkvs: kept delivery receipt %s as hidden junk — %s",
                   receipt.receipt_id, why)


def _carrier_detail(body: dict) -> str:
    """The carrier's own failure text, if it gave one.

    Shown to the operator verbatim under the message in the CRM, because "failed" alone does
    not tell them whether to try another number or wait. Read the same defensive way as
    RefId/Status above: BulkVS's DLR field names are not pinned by a contract we control.
    """
    for key in ("ErrorMessage", "Error", "StatusMessage", "Reason", "Description",
                "errorMessage", "error", "reason", "detail"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    return ""


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
        # Relayed for EVERY receipt the ladder RECOGNISES, not only the ones that moved
        # OWEN's own row. `handle_message_send` marks a row 'sent' the moment BulkVS accepts
        # it, so a carrier "sent" DLR advances nothing here while being exactly the news the
        # CRM is waiting for — its own copy is still QUEUED. Both sides apply their own
        # forward-only ladder, so a repeat or an out-of-order receipt is harmless.
        word = str(new_status or "").strip().lower()
        receipt = ({"message_id": str(msg.id), "status": word,
                    "detail": _carrier_detail(body)}
                   if word in sms.OUTBOUND_STATUS_RANK else None)

    # ADDITIVE and opt-in: this does nothing unless the message was SENT THROUGH the CRM
    # link (a marker on its `messages` row) on a DID that is still bound. An operator's text
    # from the Inbox, or a flow's, is not relayed — the CRM has no event for it and would
    # 404 every one. The hook is total, so the 200 below is reached whatever happens.
    if receipt:
        await crm_hook.handle_delivery_receipt(**receipt)
    return Response(status_code=200)

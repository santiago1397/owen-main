"""The functions the existing call and message paths call into this module.

Four of them, one per surface, and every one obeys the same contract: they are TOTAL (they
catch everything), they answer False on any failure at all, and False always means "I did
nothing — carry on exactly as before". Nothing in here can leave a caller unhandled, a text
un-ingested or a webhook without its 200.

  * `handle_bound_inbound`   — an inbound CALL on a bound DID (`flows/runtime.py`)
  * `is_new_inbound_message` — asked BEFORE the ingest upsert, so a re-delivered BulkVS
                               webhook cannot put a second copy of a text on the CRM thread
  * `handle_inbound_message` — an inbound SMS/MMS on a bound DID (`webhooks/bulkvs.py`)
  * `handle_delivery_receipt`— a carrier receipt for a message the CRM sent

## The call hook

`flows/runtime.py::run_flow_for_stasis` gains exactly three lines:

    if await crm_hook.handle_bound_inbound(ari, channel_id, lid, str(dialed), caller_number):
        return
    await _handle_unassigned(ari, channel_id, lid, str(dialed), caller_number)

placed inside the EXISTING `if not assigned:` branch — the branch that already means "this
DID has no flow, run the built-in default". That placement is the whole safety argument:

  * A DID with a flow assigned never reaches this line, so every configured call flow is
    untouched.
  * A DID with no flow and no CRM binding gets False here and falls into
    `_handle_unassigned` on the very next line, byte for byte as it does today.
  * With `CRM_LINK_ENABLED` false this returns False before touching the database, so the
    disabled system is not merely equivalent to today's — it does strictly less work.

The function is total: it catches everything, and every failure path returns False, which
means "I did not handle this call, carry on as before". There is no failure of this module
that can leave a caller unhandled.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("integrations.crm.hook")


async def handle_bound_inbound(
    ari, channel_id: str, lid: str, dialed: str, caller_number: str
) -> bool:
    """True iff the CRM link took this call. False means "not mine — carry on".

    Ordered cheapest-first so the common case (the CRM link is off, or this DID is not
    bound) costs a boolean read and one indexed query respectively.
    """
    try:
        from app.integrations.crm import config as crm_config

        if not crm_config.link_enabled():
            return False

        from app.db import SessionLocal
        from app.integrations.crm import binding as crm_binding

        async with SessionLocal() as db:
            bound = await crm_binding.resolve(db, dialed)
        if bound is None:
            return False

        from app.integrations.crm.handler import handle_bound_inbound as run
    except Exception:  # noqa: BLE001 - THE most important except in this module.
        # Anything at all going wrong here — a bad import, a dead database, a typo in a
        # config value — must hand the call back to the handler that already works rather
        # than drop it. False is the safe answer and is always available.
        logger.exception(
            "crm-link: hook failed for DID %s (linkedid=%s); falling back to default handling",
            dialed, lid,
        )
        return False

    # PAST THE POINT OF NO RETURN. From here the CRM link owns this call, and the answer is
    # True whatever happens — `handler.handle_bound_inbound` absorbs its own failures and
    # always leaves the channel answered-and-handled or hung up. Returning False after it
    # had already answered the caller would run `_handle_unassigned` on the SAME channel,
    # playing a consent notice and a voicemail greeting over a call that is already in
    # progress. A caught exception here can only mean the handler itself was unreachable.
    logger.info("crm-link: handling DID %s (linkedid=%s, link=%s)", dialed, lid, bound.link_id)
    try:
        await run(ari, channel_id, lid, dialed, caller_number, bound)
    except Exception:  # noqa: BLE001
        logger.exception("crm-link: handler raised for DID %s (linkedid=%s)", dialed, lid)
        try:
            await ari.hangup(channel_id)
        except Exception:  # noqa: BLE001
            logger.exception("crm-link: post-failure hangup failed (linkedid=%s)", lid)
    return True


# --- the message hooks --------------------------------------------------------------------
# `webhooks/bulkvs.py` gains one call on each of its two routes, and nothing else. Both are
# total in the same sense as the call hook above: any failure at all answers False, and the
# webhook goes on to return its 200 exactly as it does today. A CRM that is down, slow or
# misconfigured cannot cost us the 200 that stops BulkVS re-delivering a customer's text.


async def is_new_inbound_message(db, provider_message_sid: str) -> bool:
    """True iff this MO webhook has not been ingested before AND the link is on.

    Asked BEFORE `ingest_message_event`, which is an upsert keyed on the provider SID and
    so cannot tell the caller whether it inserted or updated. BulkVS re-delivers a POST it
    did not get a 200 for, and `POST /api/events` on the CRM has no dedupe of its own, so
    without this a retry would put a SECOND copy of the same text on the customer's CRM
    thread. The existing GoHighLevel relay has exactly this guard in `relayed_to_ghl`; this
    is its equivalent for a table we may not add a column to.

    Returns False — do not push — when the link is off, and does so BEFORE any query, so
    the disabled system does strictly less work rather than merely the same amount.
    """
    try:
        from app.integrations.crm import config as crm_config

        if not crm_config.link_enabled():
            return False
        sid = str(provider_message_sid or "").strip()
        if not sid:
            return False

        from sqlalchemy import select

        from app.models import Message

        existing = (
            await db.execute(
                select(Message.id).where(Message.provider_message_sid == sid).limit(1)
            )
        ).first()
        return existing is None
    except Exception:  # noqa: BLE001 - an unanswerable question is answered "do not push"
        logger.exception("crm-link: could not tell whether %s was already ingested",
                         provider_message_sid)
        return False


async def handle_inbound_message(
    *, message_id: str, from_number: str, dialed_number: str, body: str,
    num_media: int = 0, provider_message_sid: str = "",
) -> bool:
    """Queue an inbound text on a CRM-BOUND DID as a CRM message event.

    True iff a job was written. False means the DID is not bound, the link is off, or
    something went wrong — in all three cases the message has already been ingested and
    relayed to GoHighLevel by the caller, and nothing about that changes.

    Ordered cheapest-first, exactly like the call hook: the kill switch is a boolean read,
    the binding is one indexed query, and a DID that is not bound costs nothing more.
    """
    try:
        from app.integrations.crm import config as crm_config

        if not crm_config.link_enabled():
            return False

        from app.db import SessionLocal
        from app.integrations.crm import binding as crm_binding

        async with SessionLocal() as db:
            bound = await crm_binding.resolve(db, dialed_number)
        if bound is None:
            return False

        from app.integrations.crm import push as crm_push

        logger.info("crm-link: reporting inbound message %s on DID %s (link=%s)",
                    message_id, dialed_number, bound.link_id)
        return await crm_push.report_inbound_message(
            message_id=message_id, binding=bound, caller_number=from_number,
            dialed_number=dialed_number, body=body, num_media=num_media,
            provider_message_sid=provider_message_sid,
        )
    except Exception:  # noqa: BLE001 - the text is already ingested; this is the extra
        logger.exception("crm-link: reporting inbound message %s failed", message_id)
        return False


async def handle_delivery_receipt(*, message_id: str, status: str, detail: str = "") -> bool:
    """Relay a carrier delivery receipt for a message the CRM sent through the link.

    True iff a job was written. False — do nothing — for every one of:
      * the kill switch is off (before any query at all);
      * the `messages` row is gone, or is not OUTBOUND;
      * the row carries no CRM-link marker, i.e. an operator or a flow sent it and the CRM
        has no event for it. Relaying those would be a 404 per text, forever;
      * the DID is no longer bound;
      * anything went wrong.

    `status` is the CARRIER's word, not OWEN's advanced status. Both sides apply their own
    forward-only ladder, which is what makes a late or out-of-order receipt harmless.
    """
    try:
        from app.integrations.crm import config as crm_config

        if not crm_config.link_enabled():
            return False
        word = str(status or "").strip().lower()
        if not word or not message_id:
            return False

        import uuid as _uuid

        from app.db import SessionLocal
        from app.integrations.crm import binding as crm_binding
        from app.models import Message

        async with SessionLocal() as db:
            msg = await db.get(Message, _uuid.UUID(str(message_id)))
            if msg is None:
                return False
            marker = crm_config.marker_of(msg.raw_payload)
            if marker is None:
                # Not the CRM's message. The overwhelmingly common case on a live system,
                # and the reason this is checked before anything else is looked up.
                return False
            if (msg.direction or "").lower() != "outbound":
                logger.warning("crm-link: receipt for %s ignored — it is not an outbound row",
                               message_id)
                return False
            did = msg.from_number or marker.get("did") or ""
            sid = msg.provider_message_sid or ""
            bound = await crm_binding.resolve(db, did)
        if bound is None:
            logger.info("crm-link: not relaying the receipt for message %s — %s is no "
                        "longer bound to the CRM", message_id, did or "<no DID>")
            return False

        from app.integrations.crm import push as crm_push

        return await crm_push.report_delivery_receipt(
            message_id=str(message_id), binding=bound, status=word, detail=detail,
            dialed_number=did, provider_message_sid=sid,
        )
    except Exception:  # noqa: BLE001 - OWEN's own row is already advanced and committed
        logger.exception("crm-link: relaying the delivery receipt for %s failed", message_id)
        return False

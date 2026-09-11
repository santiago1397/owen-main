"""`/api/crm-link/*` — the two directions of the link, on the internal network only.

Nothing here is publicly routed. `callmon_app` publishes no host ports and Traefik routes
only `api.${APP_DOMAIN}`; these paths are reachable from a container on `callmon-net` or
`traefik-public` and from nowhere else. That is a topology fact, not a permission, so every
route is authenticated anyway.

## Two callers, two privilege levels

  * **The worker**, delivering a queued call event: `POST /events`, scope `agent_write`.
    That is the scope `AGENT_RUNTIME_KEY` already carries for the existing agent CRM report,
    so no new credential is needed for the internal hop.
  * **The CRM**, asking OWEN to do something to a phone line: `POST /calls`, `POST /messages`,
    `GET /health`, scope `crm_link`. A NEW scope, because `agent_write` is documented as
    "WRITE captures and notes via /api/agent-runtime/*" and using it to authorise placing a
    telephone call would falsify that description — the same objection `api/agent_runtime.py`
    itself raises about bolting writes onto the read-only `/api/ai` surface.

Every route refuses with 503 while `CRM_LINK_ENABLED` is false, before it reads a database.

## Guards, in the order they are applied to a real call or text

  1. kill switch                     `CRM_LINK_ENABLED`
  2. destination allowlist           `CRM_LINK_ALLOWLIST` — empty allows nothing
  3. the number is CRM-bound         an enabled `crm_links` row
  4. the from-number is usable       owned BulkVS DID, active, carrier-Active
  5. the contact is not blocked      the Inbox block, shared with the operator send path
  6. (SMS) the 10DLC gate            `numbers.sms_enabled` + `sms_campaign_id`
  7. (SMS) the dark switch           `CRM_LINK_SMS_ENABLED`
  8. (SMS) per-contact opt-out       `sms_opt_outs`, re-checked again at drain

Guards 4-8 are the platform's own, reused verbatim. This module adds 1-3 and never relaxes
anything: a number OWEN would refuse to text today is still refused with the link enabled.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ai.deps import require_scope
from app.core.apikeys import SCOPE_AGENT_WRITE, SCOPE_CRM_LINK
from app.core.config import settings
from app.db import get_db
from app.integrations.crm import binding as crm_binding
from app.integrations.crm import config as crm_config
from app.integrations.crm.client import CrmClient
from app.integrations.crm.events import (CallEventFacts, DeliveryReceiptFacts,
                                         MessageEventFacts, to_crm_delivery_receipt,
                                         to_crm_event, to_crm_message_event,
                                         validate_crm_delivery_receipt, validate_crm_event)
from app.models import Number
from app.services import queue, sms
from app.telephony import outbound as outbound_rules

logger = logging.getLogger("integrations.crm.api")

router = APIRouter(prefix="/api/crm-link", tags=["crm-link"])


def _require_enabled() -> crm_config.CrmLinkSettings:
    """503 while the kill switch is off. Checked before anything else on every route, so a
    disabled link never opens a database session, never resolves a number and never logs a
    customer's phone number."""
    cfg = crm_config.current()
    if not cfg.enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "the CRM link is disabled (set CRM_LINK_ENABLED=true to enable it)",
        )
    return cfg


async def _bound_from_number(db: AsyncSession, from_number: str):
    """`(Number, CrmBinding)` for a from-number that is CRM-bound and usable for outbound.

    Raises the specific refusal rather than a generic 403, because the CRM is a machine
    caller: it cannot ask a follow-up question, so the reason has to be in the response.
    """
    number = (
        await db.execute(
            select(Number).where(
                Number.phone_number == from_number,
                Number.media_provider == settings.BULKVS_MEDIA_PROVIDER,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if number is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"{from_number} is not a number OWEN carries"
        )
    # The SAME predicate the operator from-number picker applies: owned by BulkVS, active,
    # not soft-released, carrier-Active. A SUBMITTED pending port-in is not usable.
    if not outbound_rules.is_owned_bulkvs_did(
        number, owner_provider=settings.BULKVS_OWNER_PROVIDER
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{from_number} is not a usable outbound DID "
            f"(active={number.active}, carrier status={number.provider_status!r})",
        )
    bound = await crm_binding.resolve(db, from_number)
    if bound is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, crm_config.REFUSE_NOT_BOUND)
    return number, bound


# --- direction 1: OWEN -> the CRM (the worker's delivery hop) ------------------------------


class EventDeliveryIn(BaseModel):
    """The `crm_report` job body, i.e. `events.CallEventFacts.as_payload()`.

    Loose on purpose — extra keys are accepted and ignored — because a job enqueued by an
    older deploy must still drain against a newer one. The queue outlives the code.
    """

    model_config = {"extra": "allow"}

    phase: str = "ended"
    owen_call_id: str = ""
    linkedid: str = ""
    caller_number: str = ""
    dialed_number: str = ""
    direction: str = "inbound"
    outcome: str = ""
    duration_seconds: Optional[int] = None
    winning_destination: Optional[str] = None
    winning_kind: Optional[str] = None
    recording_id: Optional[str] = None
    transcript_id: Optional[str] = None
    owen_url: Optional[str] = None
    extra: dict = Field(default_factory=dict)


@router.post("/events")
async def deliver_event(
    body: EventDeliveryIn,
    db: AsyncSession = Depends(get_db),
    _key=Depends(require_scope(SCOPE_AGENT_WRITE)),
) -> dict:
    """Deliver one queued call lifecycle event to the CRM.

    Called by `workers/handlers.py::handle_crm_report` draining a `crm_report` job. The
    status code is the retry contract that handler reads:

      * **200** — delivered, or permanently undeliverable. The job completes. A payload the
        CRM refuses with a 4xx will not become acceptable on the sixth attempt.
      * **502** — the CRM was unreachable or answered 5xx. Raise so the queue retries with
        backoff and eventually dead-letters.
      * **200 with `ok: false`** for a CRM 4xx: the payload is wrong and retrying it will
        produce the same 4xx five more times. It is logged at WARNING, which surfaces it in
        `app_logs` and so in `GET /api/ai/errors`.
    """
    cfg = _require_enabled()
    facts = CallEventFacts.from_payload(body.model_dump())

    # Per-binding CRM URL/token when the call was on a bound DID, else the globals. Resolved
    # here rather than carried in the job so a rotated token takes effect on the next drain.
    base_url, token = cfg.base_url, cfg.token
    if facts.dialed_number:
        bound = await crm_binding.resolve(db, facts.dialed_number)
        if bound is not None:
            base_url, token = bound.crm_base_url or base_url, bound.crm_token or token
    if not token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, crm_config.REFUSE_NO_TOKEN)

    # ONE budget for the whole delivery — the lookup AND the POST. See CrmClient: the
    # worker posts here with a 20s client timeout, and a delivery that overruns it is
    # retried after the CRM may already have inserted the event.
    client = CrmClient(base_url, token, timeout_s=cfg.http_timeout_seconds,
                       budget_s=cfg.http_budget_seconds)
    contact_id, reason = await client.resolve_contact_id(facts.caller_number)
    if contact_id is None:
        # NOT a drop any more. An unresolved caller is now sent with `from_number` and the
        # CRM matches-or-creates the contact itself (see events.py, THE AMENDMENT). This
        # used to `return {"ok": False}` here, which is what silently discarded every
        # first-time roofing lead — the single most valuable event the business gets.
        #
        # INFO, not WARNING: "the caller is new" is the normal life of a phone line, and a
        # warning per new lead would train everyone to ignore the log. The reason is still
        # recorded, because it also covers the cases that ARE worth seeing — a token
        # without the `read` scope, or a CRM that could not be searched.
        logger.info("crm-link: no contact_id for %s event on call %s (%s) — sending "
                    "from_number=%s for the CRM to match or create",
                    facts.phase, facts.owen_call_id or facts.linkedid, reason,
                    facts.caller_number or "<unknown>")

    crm_body = to_crm_event(facts, contact_id)
    problems = validate_crm_event(crm_body)
    if problems:
        # Our own bug, caught before it becomes a 400 in a retry loop. Not retryable.
        logger.error("crm-link: refusing to send a malformed event: %s", problems)
        return {"ok": False, "reason": "; ".join(problems), "phase": facts.phase}

    result = await client.post_event(crm_body)
    if result.ok:
        return {"ok": True, "phase": facts.phase, "contact_id": contact_id,
                "crm_status": result.status}
    if result.retryable:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"CRM did not accept the event ({result.status}): {result.reason}",
        )
    return {"ok": False, "reason": f"CRM {result.status}: {result.reason}",
            "phase": facts.phase}


class MessageDeliveryIn(BaseModel):
    """The `crm_report` job body for a message, i.e. `events.MessageEventFacts.as_payload()`.

    Loose for the same reason `EventDeliveryIn` is: a job enqueued by an older deploy has to
    drain against a newer one.
    """

    model_config = {"extra": "allow"}

    owen_message_id: str = ""
    caller_number: str = ""
    dialed_number: str = ""
    body: str = ""
    direction: str = "inbound"
    num_media: int = 0
    provider_message_sid: str = ""
    extra: dict = Field(default_factory=dict)


@router.post("/message-events")
async def deliver_message_event(
    body: MessageDeliveryIn,
    db: AsyncSession = Depends(get_db),
    _key=Depends(require_scope(SCOPE_AGENT_WRITE)),
) -> dict:
    """Deliver one queued SMS/MMS to the CRM as a `type: "SMS"` event.

    The message sibling of `deliver_event`, with the SAME retry contract, because the same
    `handle_crm_report` handler drains both and reads the status code the same way:
    200 completes the job, 502 makes the queue retry with backoff.

    The bound-DID check is repeated HERE, at drain time, and not merely trusted from the
    webhook that enqueued the job. A binding can be disabled between enqueue and drain —
    that is the whole point of having a per-number switch — and a job already in the queue
    must not keep pushing a customer's texts to a CRM the owner has just unbound.
    """
    cfg = _require_enabled()
    facts = MessageEventFacts.from_payload(body.model_dump())

    base_url, token = cfg.base_url, cfg.token
    bound = await crm_binding.resolve(db, facts.dialed_number) if facts.dialed_number else None
    if bound is None:
        logger.warning("crm-link: not delivering message %s — %s is not bound to the CRM",
                       facts.owen_message_id, facts.dialed_number or "<no DID>")
        return {"ok": False, "reason": crm_config.REFUSE_NOT_BOUND,
                "message_id": facts.owen_message_id}
    base_url, token = bound.crm_base_url or base_url, bound.crm_token or token
    if not token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, crm_config.REFUSE_NO_TOKEN)

    client = CrmClient(base_url, token, timeout_s=cfg.http_timeout_seconds,
                       budget_s=cfg.http_budget_seconds)
    contact_id, reason = await client.resolve_contact_id(facts.caller_number)
    if contact_id is None:
        # Same rule as a call, and it matters more here: a text from a number nobody has
        # ever called from is a lead writing in, and the CRM creates the contact for it.
        logger.info("crm-link: no contact_id for message %s (%s) — sending from_number=%s",
                    facts.owen_message_id, reason, facts.caller_number or "<unknown>")

    crm_body = to_crm_message_event(facts, contact_id)
    problems = validate_crm_event(crm_body)
    if problems:
        logger.error("crm-link: refusing to send a malformed message event: %s", problems)
        return {"ok": False, "reason": "; ".join(problems),
                "message_id": facts.owen_message_id}

    result = await client.post_event(crm_body)
    if result.ok:
        return {"ok": True, "message_id": facts.owen_message_id, "contact_id": contact_id,
                "crm_status": result.status}
    if result.retryable:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"CRM did not accept the message ({result.status}): {result.reason}",
        )
    return {"ok": False, "reason": f"CRM {result.status}: {result.reason}",
            "message_id": facts.owen_message_id}


class DeliveryReceiptIn(BaseModel):
    """The `crm_report` job body for a receipt, i.e. `events.DeliveryReceiptFacts.as_payload()`."""

    model_config = {"extra": "allow"}

    owen_message_id: str = ""
    status: str = ""
    detail: str = ""
    dialed_number: str = ""
    provider_message_sid: str = ""
    extra: dict = Field(default_factory=dict)


@router.post("/delivery-receipts")
async def deliver_receipt(
    body: DeliveryReceiptIn,
    db: AsyncSession = Depends(get_db),
    _key=Depends(require_scope(SCOPE_AGENT_WRITE)),
) -> dict:
    """Relay one carrier delivery receipt to the CRM — "i want to know if the text arrived".

    `provider_ref` carries `messages.id`, which is the id OWEN handed the CRM when it
    accepted the send (`send_message` below answers `{"message_id": ...}`) and the ONLY key
    the CRM can match a receipt on. The BulkVS RefId is never sent: the CRM has never seen
    one. The webhook resolves the RefId to the `messages` row before this is reached.

    Retry contract, as everywhere else on this router: 502 for a CRM that is down, 200 with
    `ok: false` for anything the CRM understood and refused. A **404** is in the second
    group and is an ordinary answer, not a fault — it is what a receipt for a text sent
    before the link existed looks like, and no number of retries will conjure the row.
    """
    cfg = _require_enabled()
    facts = DeliveryReceiptFacts.from_payload(body.model_dump())

    base_url, token = cfg.base_url, cfg.token
    bound = await crm_binding.resolve(db, facts.dialed_number) if facts.dialed_number else None
    if bound is None:
        logger.warning("crm-link: not relaying the receipt for message %s — %s is not bound",
                       facts.owen_message_id, facts.dialed_number or "<no DID>")
        return {"ok": False, "reason": crm_config.REFUSE_NOT_BOUND,
                "message_id": facts.owen_message_id}
    base_url, token = bound.crm_base_url or base_url, bound.crm_token or token
    if not token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, crm_config.REFUSE_NO_TOKEN)

    crm_body = to_crm_delivery_receipt(facts)
    problems = validate_crm_delivery_receipt(crm_body)
    if problems:
        logger.error("crm-link: refusing to send a malformed delivery receipt: %s", problems)
        return {"ok": False, "reason": "; ".join(problems),
                "message_id": facts.owen_message_id}

    client = CrmClient(base_url, token, timeout_s=cfg.http_timeout_seconds,
                       budget_s=cfg.http_budget_seconds)
    result = await client.post_delivery_receipt(crm_body)
    if result.ok:
        return {"ok": True, "message_id": facts.owen_message_id,
                "status": facts.status, "crm_status": result.status,
                # False when the CRM correctly ignored a stale or out-of-order receipt.
                "advanced": (result.data or {}).get("advanced")}
    if result.retryable:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"CRM did not accept the receipt ({result.status}): {result.reason}",
        )
    return {"ok": False, "reason": f"CRM {result.status}: {result.reason}",
            "message_id": facts.owen_message_id}


# --- direction 2: the CRM -> OWEN ---------------------------------------------------------


class OutboundCallIn(BaseModel):
    from_number: str          # the bound DID to call FROM (E.164)
    to_number: str            # who to call (E.164) — must be on the allowlist
    operator: Optional[str] = None   # whose softphone rings first; defaults to the binding's


@router.post("/calls")
async def place_call(
    body: OutboundCallIn,
    db: AsyncSession = Depends(get_db),
    _key=Depends(require_scope(SCOPE_CRM_LINK)),
) -> dict:
    """Place an outbound call from a bound DID, on the CRM's behalf.

    Reuses the platform's manual-outbound path exactly: it enqueues the SAME `outbound_call`
    job `api/telephony.py` enqueues, which the worker drives through
    `AsteriskAriClient.run_outbound_call` — ring the operator's softphone, ring the callee
    over the trunk with the DID as caller-ID, play the consent notice to the callee, bridge,
    record, and tear both legs down when either hangs up.

    Writing a second originate path was the alternative and was rejected: `run_outbound_call`
    carries the fixes for the bridge-before-Stasis race and the orphaned far leg that
    outlived a hangup, and a parallel implementation would reproduce both.
    """
    cfg = _require_enabled()
    if not settings.ASTERISK_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "telephony is not enabled")

    # THE ALLOWLIST, first and unconditional.
    refusal = cfg.destination_refusal(body.to_number)
    if refusal:
        logger.warning("crm-link: REFUSED outbound call to %s — %s", body.to_number, refusal)
        raise HTTPException(status.HTTP_403_FORBIDDEN, refusal)

    number, bound = await _bound_from_number(db, body.from_number)

    from app.providers.bulkvs import _to_e164
    from app.services.messages import is_contact_blocked

    callee = _to_e164(body.to_number)
    if await is_contact_blocked(db, callee):
        raise HTTPException(status.HTTP_409_CONFLICT, "this contact is blocked in OWEN")

    operator = (body.operator or bound.outbound_operator or "").strip()
    if not operator:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "no operator to ring: pass `operator`, or set outbound_operator on the binding",
        )

    op_channel_id = uuid.uuid4().hex
    callee_channel_id = uuid.uuid4().hex
    await queue.enqueue(db, "outbound_call", {
        "operator_id": operator,
        "callee_number": callee,
        "from_number": number.phone_number,
        "trunk_name": settings.BULKVS_TRUNK_NAME,
        "consent_media": settings.OUTBOUND_CONSENT_MEDIA or None,
        "record": settings.OUTBOUND_RECORDING_ENABLED,
        "op_channel_id": op_channel_id,
        "callee_channel_id": callee_channel_id,
    })
    logger.info("crm-link: queued outbound call %s -> %s via operator %s",
                number.phone_number, callee, operator)
    return {"ok": True, "operator_channel": op_channel_id,
            "callee_channel": callee_channel_id, "linkedid": op_channel_id}


class SendMessageIn(BaseModel):
    from_number: str
    to_number: str
    body: str


@router.post("/messages")
async def send_message(
    body: SendMessageIn,
    db: AsyncSession = Depends(get_db),
    _key=Depends(require_scope(SCOPE_CRM_LINK)),
) -> dict:
    """Send an SMS from a bound DID. **DARK BY DEFAULT — see `CRM_LINK_SMS_ENABLED`.**

    Why it is dark rather than absent: the DID's 10DLC campaign is SUBMITTED and not
    approved, so carriers would filter the message whatever this code did. Shipping the path
    now means the day approval lands is a config flip and a test call, not a build.

    Turning it on takes three deliberate steps, and every one of them is a refusal today:
      1. `CRM_LINK_SMS_ENABLED=true`;
      2. `numbers.sms_enabled = true` AND `numbers.sms_campaign_id = '<the 10DLC id>'` on
         the bound DID (the platform's own gate, `services/sms.outbound_block_reason`);
      3. the destination on `CRM_LINK_ALLOWLIST`.

    KNOWN UNKNOWN, flagged rather than hidden: `providers/bulkvs_client.send_message` records
    that its `/messageSend` request shape has NEVER been exercised against the live BulkVS
    API. It follows the published docs. It is not proven. The first real send is a test, not
    a rollout.
    """
    cfg = _require_enabled()

    refusal = cfg.sms_refusal(body.to_number)
    if refusal:
        logger.warning("crm-link: REFUSED SMS to %s — %s", body.to_number, refusal)
        raise HTTPException(status.HTTP_403_FORBIDDEN, refusal)

    number, bound = await _bound_from_number(db, body.from_number)

    # The platform's OWN 10DLC gate. Independent of ours and never bypassed.
    gate = sms.outbound_block_reason(number.sms_enabled, number.sms_campaign_id)
    if gate:
        raise HTTPException(status.HTTP_409_CONFLICT, gate)

    text = (body.body or "").strip()
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "message body is empty")

    from app.providers.bulkvs import _to_e164
    from app.services.messages import (enqueue_outbound_message, get_optout_state,
                                       is_contact_blocked)

    contact = _to_e164(body.to_number)
    if await is_contact_blocked(db, contact):
        raise HTTPException(status.HTTP_409_CONFLICT, "this contact is blocked in OWEN")
    if sms.is_opted_out(await get_optout_state(db, number.id, contact)):
        raise HTTPException(status.HTTP_409_CONFLICT, "this contact has opted out of SMS")

    msg = await enqueue_outbound_message(db, number, contact, text, None)
    # Mark the row as the CRM's BEFORE the send job can drain, so a delivery receipt that
    # comes back fast still finds the marker. This is the ONLY thing that distinguishes a
    # message the CRM sent from one an operator (or a flow) sent on the same DID, and
    # `/webhooks/bulkvs/message-status` relays a receipt for the first and not the second.
    # `raw_payload` is NULL on every outbound row today — see config.py, THE MARKER.
    msg.raw_payload = crm_config.link_marker(bound.link_id, number.phone_number)
    await db.commit()
    await queue.enqueue(db, "message_send", {"message_id": str(msg.id)})
    logger.info("crm-link: queued SMS %s -> %s (message %s)",
                number.phone_number, contact, msg.id)
    # `message_id` IS the correlation field: the CRM stores it as its ConversationEvent's
    # `provider_ref`, and every delivery receipt for this text comes back carrying it.
    return {"ok": True, "message_id": str(msg.id), "status": "queued"}


@router.get("/health")
async def link_health(
    db: AsyncSession = Depends(get_db),
    _key=Depends(require_scope(SCOPE_CRM_LINK)),
) -> dict:
    """Is the link up, and can this container actually reach the CRM?

    The `crm_reachable` field is the answer to the one thing that could not be verified when
    this was built: that `http://ghl_clone_api:8000` resolves from inside `callmon_app` over
    the shared `traefik-public` network. Hit this first after deploying.
    """
    cfg = _require_enabled()
    from app.integrations.crm.models import CrmLink

    rows = (
        await db.execute(
            select(CrmLink, Number).join(Number, Number.id == CrmLink.number_id)
        )
    ).all()
    probe = await CrmClient(cfg.base_url, cfg.token or "probe",
                            timeout_s=cfg.http_timeout_seconds,
                            budget_s=cfg.http_budget_seconds).probe()
    return {
        "enabled": cfg.enabled,
        "sms_enabled": cfg.sms_enabled,
        "crm_base_url": cfg.base_url,
        "crm_token_configured": bool(cfg.token),
        "crm_reachable": probe.ok,
        "crm_probe": {"status": probe.status, "reason": probe.reason},
        "allowlist_size": len(cfg.allowlist),
        "bindings": [
            {"phone_number": n.phone_number, "enabled": bool(link.enabled),
             "ring_operators": bool(link.ring_operators),
             "operator_ids": link.operator_ids or [],
             "pstn_numbers": link.pstn_numbers or [],
             "pstn_allowed": cfg.filter_pstn(link.pstn_numbers)[0]}
            for link, n in rows
        ],
    }

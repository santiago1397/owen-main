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
    `POST /softphone/credentials`, `GET /health`, scope `crm_link`. A NEW scope, because
    `agent_write` is documented as
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
from app.integrations.crm import softphone as crm_softphone
from app.integrations.crm.client import CrmClient
from app.integrations.crm.events import (CallEventFacts, to_crm_event, validate_crm_event)
from app.models import Number
from app.services import queue, sms
from app.telephony import outbound as outbound_rules
from app.telephony.credentials import build_webrtc_credentials

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

      * **200** — delivered, or permanently undeliverable (no matching contact). The job
        completes. A caller who is not in the CRM will not become one on the sixth attempt.
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
        logger.warning("crm-link: dropping %s event for call %s — %s",
                       facts.phase, facts.owen_call_id or facts.linkedid, reason)
        return {"ok": False, "reason": reason, "phase": facts.phase}

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


class SoftphoneCredentialsIn(BaseModel):
    """The CRM user who wants to become a ring destination. Their EMAIL is the whole
    request: it is the identity OWEN maps to an operator, and nothing else about a CRM
    user is meaningful here."""

    email: str


@router.post("/softphone/credentials")
async def softphone_credentials(
    body: SoftphoneCredentialsIn,
    _key=Depends(require_scope(SCOPE_CRM_LINK)),
) -> dict:
    """Mint short-lived SIP + TURN credentials so a CRM user's BROWSER can register as an
    OWEN operator, and therefore be one of the phones `ring.py` rings.

    This exists because `POST /api/telephony/webrtc/credentials` is gated on OWEN's own app
    login (`current_user`), and a CRM user does not have one. The gate here is the CRM-link
    API key, exactly like every other route in this module — the CRM's backend holds it and
    the CRM browser never sees it.

    It does NOT mint anything itself. `telephony.credentials.build_webrtc_credentials` is
    the one minting path in this codebase and is called verbatim, with the same settings the
    login-time endpoint passes; a second implementation would be a second place for the TURN
    HMAC and the endpoint naming to drift.

    The order of the guards is the contract:
      1. the kill switch (503) — before any resolution, any settings read, any logging;
      2. telephony off (503) — credentials for a dark platform are a lie, not a courtesy;
      3. the operator roster (403) — an unprovisioned email is refused by name rather than
         handed a blob that would fail to register with a bare SIP 401.

    NOTHING minted here is logged. The log line carries the slug and the expiry, which is
    what an operator debugging "why is my browser not ringing" needs, and neither the SIP
    password nor the TURN credential, which is what an `app_logs` reader must never get.
    """
    cfg = _require_enabled()
    if not settings.ASTERISK_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "telephony is not enabled")

    slug, refusal = crm_softphone.resolve_operator(body.email, cfg.softphone_operators)
    if refusal:
        # The slug, not the email: the reason has to be greppable in a log without putting
        # a staff address in every WARNING line.
        logger.warning("crm-link: REFUSED softphone credentials for %r — %s",
                       slug or "(blank)", refusal)
        code = (status.HTTP_422_UNPROCESSABLE_ENTITY
                if refusal == crm_softphone.REFUSE_NO_EMAIL
                else status.HTTP_403_FORBIDDEN)
        raise HTTPException(code, refusal)

    sip_ttl = crm_softphone.capped_ttl(
        cfg.softphone_ttl_seconds, settings.OPERATOR_SIP_TTL_SECONDS
    )
    turn_ttl = crm_softphone.capped_ttl(cfg.softphone_ttl_seconds, settings.TURN_TTL_SECONDS)
    creds = build_webrtc_credentials(
        operator_id=slug,
        sip_secret=settings.OPERATOR_SIP_SECRET,
        sip_domain=settings.OPERATOR_SIP_DOMAIN,
        wss_url=settings.OPERATOR_WSS_URL,
        turn_secret=settings.TURN_STATIC_SECRET,
        turn_urls=settings.turn_urls,
        sip_ttl_seconds=sip_ttl,
        turn_ttl_seconds=turn_ttl,
    )
    logger.info("crm-link: minted softphone credentials for operator %s (sip_ttl=%ds)",
                slug, sip_ttl)
    # `operator` is echoed so the CRM can show WHICH operator it registered as. An operator
    # who thinks they are one endpoint and are really another is the failure this makes
    # visible — the CRM shows the slug next to the registration state.
    return {"ok": True, "operator": slug,
            "endpoint": crm_softphone.endpoint_for(slug), **creds}


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

    number, _bound = await _bound_from_number(db, body.from_number)

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
    await queue.enqueue(db, "message_send", {"message_id": str(msg.id)})
    logger.info("crm-link: queued SMS %s -> %s (message %s)",
                number.phone_number, contact, msg.id)
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

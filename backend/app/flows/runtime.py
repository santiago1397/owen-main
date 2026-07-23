"""DB-aware glue that runs the pure FlowInterpreter against a live ARI call (Ticket 07).

Kept OUT of app/flows/interpreter.py so the interpreter core stays import-light (stdlib
only) and unit-testable in the sandbox. This module needs sqlalchemy, so it is imported
LAZILY from the consumer. Responsibilities on a StasisStart:
  1. Resolve the dialed DID (+E.164) -> numbers.flow_id -> the flow's ACTIVE flow_version.
     Numbers are keyed by phone_number + media_provider (BulkVS DIDs are owned by the
     'bulkvs' provider row but carry their MEDIA on 'asterisk'), NOT by the call's
     provider_id — mirrors the split-identity note on the Number model.
  2. PIN that flow_version_id onto the call (pin-once, like campaign_id at ingest) so
     downstream projection/analysis can attribute the version.
  3. Run the interpreter with a DB-backed emit() that writes ONE call_event per node
     transition (same event-sourced projection as ticket 04/05).

Each emit() uses its own short-lived session + commit so the (possibly minutes-long) call
never holds one transaction open.
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agents.service import build_spec
from app.agents.session import AgentCallContext, get_session_for_agent
from app.core.config import settings
from app.db import SessionLocal
from app.flows.interpreter import AriControl, FlowInterpreter
from app.models import Agent, AgentVersion, Call, CallEvent, Flow, FlowVersion, Number
from app.providers.asterisk import linkedid as _linkedid
from app.services import queue, sms
from app.services.ingestion import _get_or_create_provider
from app.services.messages import enqueue_outbound_message, get_optout_state
from app.services.number_sync import is_carrier_active

logger = logging.getLogger("flows.runtime")

PROVIDER_NAME = "asterisk"  # matches asterisk_consumer.PROVIDER_NAME (call row / call_events)

# `request` node: hard wall-clock ceiling on the whole HTTP exchange (Ticket 17).
_REQUEST_TIMEOUT_S = 5.0

_DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _builtin_variables(caller_number: str, dialed_number: str) -> dict:
    """Seed the interpreter's per-call variable store (Ticket 17). call.time / call.dow are
    computed HERE (app code, not the pure interpreter) in the business timezone."""
    try:
        local = datetime.now(ZoneInfo(settings.BUSINESS_TZ))
    except Exception:  # noqa: BLE001 - a misconfigured tz must not break call handling
        local = datetime.now(timezone.utc)
    return {
        "caller_number": caller_number,
        "dialed_number": dialed_number,
        "call.time": local.strftime("%H:%M"),
        "call.dow": _DOW[local.weekday()],
    }


async def _send_flow_sms(dialed_number: str, to: str, body: str, lid: str) -> None:
    """Background task behind the `send_sms` node: enqueue an outbound SMS FROM the flow's
    DID through the platform outbound path, so the SAME gates as a manual send apply
    (per-number 10DLC, carrier-active, per-contact opt-out; the worker re-checks opt-out at
    drain). Own short-lived session (the runtime's session-per-emit pattern); best-effort —
    any refusal/failure is logged and the call is never affected."""
    try:
        async with SessionLocal() as db:
            number = (
                await db.execute(
                    select(Number).where(
                        Number.phone_number == dialed_number,
                        Number.media_provider == settings.BULKVS_MEDIA_PROVIDER,
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if number is None:
                logger.warning("flow send_sms: no Number row for DID %s (linkedid=%s)", dialed_number, lid)
                return
            reason = sms.outbound_block_reason(number.sms_enabled, number.sms_campaign_id)
            if reason:
                logger.info("flow send_sms: refused for %s — %s (linkedid=%s)", dialed_number, reason, lid)
                return
            if not is_carrier_active(number.provider_status):
                logger.info(
                    "flow send_sms: %s not carrier-active (status=%s, linkedid=%s)",
                    dialed_number, number.provider_status, lid,
                )
                return
            state = await get_optout_state(db, number.id, to)
            if sms.is_opted_out(state):
                logger.info("flow send_sms: %s opted out of %s (linkedid=%s)", to, dialed_number, lid)
                return
            msg = await enqueue_outbound_message(db, number, to, body, None)
            await queue.enqueue(db, "message_send", {"message_id": str(msg.id)})
            logger.info("flow send_sms: queued %s -> %s (linkedid=%s)", dialed_number, to, lid)
    except Exception:  # noqa: BLE001 - fire-and-forget: never raises into anything
        logger.exception("flow send_sms: failed (DID %s -> %s, linkedid=%s)", dialed_number, to, lid)


async def _http_request(method: str, url: str, headers: dict, body: Any) -> tuple[int, Any]:
    """The `request` node's transport seam: one HTTP exchange under a HARD 5s ceiling.

    Returns (status, parsed_body) — parsed JSON when the response is JSON, else the text.
    Any transport error / timeout -> (0, None), which the interpreter maps to the `failure`
    port with request.status = 0. Never raises."""
    async def _do() -> tuple[int, Any]:
        kwargs: dict = {}
        if method == "POST" and body is not None:
            if isinstance(body, (dict, list)):
                kwargs["json"] = body
            else:
                text = str(body)
                try:
                    kwargs["json"] = json.loads(text)
                except ValueError:
                    kwargs["content"] = text
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
            resp = await client.request(method, url, headers=headers or None, **kwargs)
        try:
            parsed: Any = resp.json()
        except ValueError:
            parsed = resp.text
        return resp.status_code, parsed

    try:
        return await asyncio.wait_for(_do(), timeout=_REQUEST_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - timeout/transport error -> failure port
        logger.warning("flow request: %s %s failed: %r", method, url, exc)
        return 0, None


async def _resolve_active_flow_version(
    db, dialed_number: str
) -> tuple[bool, Optional[tuple[uuid.UUID, dict]]]:
    """(assigned, resolved) for the dialed DID.

    `assigned` is True iff the number row carries a flow_id — the operator INTENDED a flow
    to answer this DID. `resolved` is (active flow_version_id, graph) when that intent is
    runnable, else None. The split matters for the Ticket 15.6 safety net: an unassigned
    number is a silent no-op, but an ASSIGNED number whose flow fails to resolve must
    blind-forward rather than dead-air."""
    number = (
        await db.execute(
            select(Number).where(
                Number.phone_number == dialed_number,
                Number.media_provider == settings.BULKVS_MEDIA_PROVIDER,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if number is None or number.flow_id is None:
        return False, None

    flow = (
        await db.execute(select(Flow).where(Flow.id == number.flow_id))
    ).scalar_one_or_none()
    if flow is None or flow.active_version_id is None:
        return True, None

    fv = (
        await db.execute(select(FlowVersion).where(FlowVersion.id == flow.active_version_id))
    ).scalar_one_or_none()
    if fv is None or not isinstance(fv.graph, dict):
        return True, None
    return True, (fv.id, fv.graph)


async def _pin_flow_version(db, provider_id: int, provider_call_sid: str, fv_id: uuid.UUID) -> None:
    """Pin-once: set calls.flow_version_id only while still NULL (never re-attribute)."""
    await db.execute(
        update(Call)
        .where(
            Call.provider_id == provider_id,
            Call.provider_call_sid == provider_call_sid,
            Call.flow_version_id.is_(None),
        )
        .values(flow_version_id=fv_id)
    )


async def _resolve_active_agent_version(db, agent_id) -> Optional[AgentVersion]:
    """The agent's ACTIVE version row, or None if the agent/version is missing (Ticket 11).
    The `ai_agent` node references an agent by id; the specific version is resolved (and
    pinned) at node entry — mirroring how a flow's active version is resolved at StasisStart."""
    agent = (
        await db.execute(select(Agent).where(Agent.id == agent_id))
    ).scalar_one_or_none()
    if agent is None or agent.active_version_id is None:
        return None
    return (
        await db.execute(select(AgentVersion).where(AgentVersion.id == agent.active_version_id))
    ).scalar_one_or_none()


async def _pin_agent_version(db, provider_id: int, provider_call_sid: str, av_id: uuid.UUID) -> None:
    """Pin-once: set calls.agent_version_id only while still NULL (like the flow_version pin)."""
    await db.execute(
        update(Call)
        .where(
            Call.provider_id == provider_id,
            Call.provider_call_sid == provider_call_sid,
            Call.agent_version_id.is_(None),
        )
        .values(agent_version_id=av_id)
    )


async def _emit_node_event(
    db, provider_id: int, provider_call_sid: str, event_type: str,
    provider_sequence: str, payload: dict,
) -> None:
    """Append one call_event for a node transition (dedup on the natural key)."""
    call = (
        await db.execute(
            select(Call).where(
                Call.provider_id == provider_id,
                Call.provider_call_sid == provider_call_sid,
            )
        )
    ).scalar_one_or_none()
    if call is None:
        # The StasisStart status event creates the call row before we run; if it is somehow
        # absent there is nothing to attach the transition to.
        return
    await db.execute(
        pg_insert(CallEvent)
        .values(
            call_id=call.id,
            event_type=event_type,
            provider_sequence=provider_sequence,
            payload=payload,
        )
        .on_conflict_do_nothing(index_elements=["call_id", "event_type", "provider_sequence"])
    )


async def run_flow_for_stasis(event: dict, ari: AriControl) -> None:
    """Entry point the consumer calls on an entry-channel StasisStart. Best-effort: any
    failure is logged, never raised into the WS loop (the consumer also guards this)."""
    ch = event.get("channel") if isinstance(event.get("channel"), dict) else {}
    channel_id = str(ch.get("id") or "")
    lid = _linkedid(event)
    dialed = (ch.get("dialplan") or {}).get("exten") if isinstance(ch.get("dialplan"), dict) else None
    caller_number = str((ch.get("caller") or {}).get("number") or "") if isinstance(ch.get("caller"), dict) else ""
    if not channel_id or not lid or not dialed:
        return

    try:
        async with SessionLocal() as db:
            assigned, resolved = await _resolve_active_flow_version(db, str(dialed))
            if resolved is not None:
                provider = await _get_or_create_provider(db, PROVIDER_NAME)
                provider_id = provider.id
                await db.commit()  # ensure the 'asterisk' provider row exists before the flow runs
    except Exception:  # noqa: BLE001 - a DB hiccup on an assigned DID must not dead-air
        logger.exception("flow runtime: flow resolution failed for DID %s (linkedid=%s)",
                         dialed, lid)
        assigned, resolved = True, None

    if not assigned:
        # Ticket 18: the DID has NO flow assigned -> the built-in default call handling
        # (consent notice -> ring every AVAILABLE operator -> first-answer bridge -> else
        # voicemail). Run with NO DB session held (it can bridge/record for minutes). A real
        # assigned flow OVERRIDES this; this is what happens before anything is configured.
        logger.info("flow runtime: no assigned flow for DID %s (linkedid=%s); default handling",
                    dialed, lid)
        await _handle_unassigned(ari, channel_id, lid, str(dialed), caller_number)
        return

    if resolved is None:
        # Ticket 15.6 safety net: the DID is flow-assigned but the flow didn't resolve
        # (deleted flow / no active version / malformed graph / DB error). Never dead
        # air: blind-forward to the global fallback number if configured. (Called with no
        # session held open — the forward may bridge for minutes.)
        logger.error(
            "FLOW FALLBACK: DID %s has a flow assigned but no runnable active version "
            "(linkedid=%s); blind-forwarding", dialed, lid,
        )
        await _fallback_forward(ari, channel_id, lid)
        return
    fv_id, graph = resolved

    async def pin() -> None:
        # StasisStart pin (interpreter on_start hook): pin-once, own short session.
        async with SessionLocal() as dbp:
            await _pin_flow_version(dbp, provider_id, lid, fv_id)
            await dbp.commit()

    async def emit(event_type: str, provider_sequence: str, payload: dict) -> None:
        async with SessionLocal() as db2:
            await _emit_node_event(db2, provider_id, lid, event_type, provider_sequence, payload)
            await db2.commit()

    async def run_agent(node: dict) -> tuple[str, dict]:
        # ai_agent node entry (Ticket 11): resolve + PIN the node's agent version, run a
        # VoiceAgentSession (dummy by default; kill-switch/per-agent engine), return its exit
        # PORT + tool data. The agent never bridges — the interpreter routes by the port.
        # Any failure -> ("failed", {}) so the node takes its `failed` port (then fallback).
        agent_id = node.get("agent_id") or node.get("agent")
        if not agent_id:
            logger.warning("flow runtime: ai_agent node has no agent_id (linkedid=%s)", lid)
            return ("failed", {})
        try:
            async with SessionLocal() as dba:
                version = await _resolve_active_agent_version(dba, agent_id)
                if version is None:
                    logger.info("flow runtime: agent %s has no active version (linkedid=%s)", agent_id, lid)
                    return ("failed", {})
                await _pin_agent_version(dba, provider_id, lid, version.id)
                await dba.commit()
                spec = build_spec(str(version.agent_id), str(version.id), version.config)
            session = get_session_for_agent(spec)
            ctx = AgentCallContext(channel_id=channel_id, linkedid=lid, ari=ari)
            result = await session.run(spec, ctx)
            return (result.port, result.data)
        except Exception:  # noqa: BLE001 - never dead-air; the node takes `failed`/fallback
            logger.exception("flow runtime: ai_agent run failed (linkedid=%s)", lid)
            return ("failed", {})

    async def send_sms(to: str, body: str) -> bool:
        # send_sms node (Ticket 17): fire-and-forget. Schedule the gated enqueue on its own
        # task + session so the interpreter takes its `default` port immediately.
        asyncio.create_task(_send_flow_sms(str(dialed), to, body, lid))
        return True

    interpreter = FlowInterpreter(
        graph=graph,
        channel_id=channel_id,
        ari=ari,
        emit=emit,
        linkedid=lid,
        business_tz=settings.BUSINESS_TZ,
        on_start=pin,
        run_agent=run_agent,
        send_sms=send_sms,
        http_request=_http_request,
        variables=_builtin_variables(caller_number, str(dialed)),
    )
    logger.info("flow runtime: running flow_version=%s on linkedid=%s DID=%s", fv_id, lid, dialed)
    try:
        await interpreter.run()
    except Exception:  # noqa: BLE001 - Ticket 15.6: an interpreter crash must never dead-air
        # The interpreter absorbs per-node failures itself, so reaching here means it blew
        # up before/at the entry node (bad graph shape, infrastructure failure). Loudly
        # blind-forward the caller instead of leaving dead air.
        logger.exception(
            "FLOW FALLBACK: interpreter crashed for flow_version=%s (linkedid=%s); "
            "blind-forwarding", fv_id, lid,
        )
        await _fallback_forward(ari, channel_id, lid)


async def _handle_unassigned(
    ari, channel_id: str, lid: str, dialed: str, caller_number: str
) -> None:
    """Ticket 18 — the default behavior for a BulkVS/Asterisk DID with NO flow assigned.

    Sequence (never dead air at any step; every ARI op is best-effort in the client):
      1. answer the caller;
      2. play the recording-consent notice to completion (FL all-party consent), if configured;
      3. if ring-operators is enabled AND at least one operator softphone is registered, ring
         them ALL at once (caller hears ringback), bridge the caller to the first to answer,
         record the bridged call, and end when either side hangs up;
      4. otherwise (no operators available, nobody answered in time, or ringing disabled) take
         a voicemail — greeting, beep, record until hangup/silence, which rides the existing
         recording pipeline and surfaces in the Inbox.

    `ari` is the concrete AsteriskAriClient (has available_operators / ring_* / voicemail);
    typed loosely because the pure AriControl protocol doesn't declare those platform ops."""
    try:
        await ari.answer(channel_id)

        consent = (settings.INBOUND_CONSENT_MEDIA or "").strip()
        if consent:
            await ari.play_and_wait(channel_id, consent)

        if settings.NO_FLOW_RING_OPERATORS:
            operators = await ari.available_operators()
            if operators:
                # Caller-ID on each operator leg: display name = the dialed DID (so the popup can
                # show "to what number"), number = the caller (who is calling). The frontend
                # enriches both to contact/campaign names.
                caller_id = f"{dialed} <{caller_number}>" if caller_number else dialed
                record_name = f"{lid}-operator-1" if settings.INBOUND_RECORDING_ENABLED else None
                await ari.ring_start(channel_id)
                try:
                    result = await ari.ring_and_bridge(
                        channel_id,
                        operators,
                        caller_id=caller_id,
                        timeout_s=float(settings.OPERATOR_RING_TIMEOUT_SECONDS),
                        record_name=record_name,
                    )
                finally:
                    await ari.ring_stop(channel_id)
                if result == "answered":
                    await ari.hangup(channel_id)  # bridge ended -> end the call
                    return
                logger.info(
                    "default handling: operators did not answer (%s) for DID %s (linkedid=%s); "
                    "-> voicemail", result, dialed, lid,
                )
            else:
                logger.info(
                    "default handling: no operators available for DID %s (linkedid=%s); "
                    "-> voicemail", dialed, lid,
                )

        await ari.voicemail(
            channel_id,
            greeting=settings.VOICEMAIL_GREETING,
            name=f"{lid}-vm-1",
            max_duration_s=float(settings.VOICEMAIL_MAX_DURATION_SECONDS),
            max_silence_s=float(settings.VOICEMAIL_MAX_SILENCE_SECONDS),
        )
    except Exception:  # noqa: BLE001 - the default handler must never raise into the consumer
        logger.exception("default handling failed for DID %s (linkedid=%s)", dialed, lid)
        try:
            await ari.hangup(channel_id)
        except Exception:  # noqa: BLE001
            logger.exception("default handling: final hangup failed (linkedid=%s)", lid)


async def _fallback_forward(ari: AriControl, channel_id: str, lid: str) -> None:
    """Ticket 15.6 safety net: blind-forward a flow-assigned call whose flow failed.

    Answer the channel and dial+bridge `FLOW_FALLBACK_FORWARD_NUMBER` (reusing the Ticket
    15.3 dial machinery — `dial_number` blocks until either leg hangs up), then hang up.
    With no fallback number configured the best we can do is a clean hangup — still never
    dead air. Best-effort throughout; never raises into the consumer."""
    fallback = (settings.FLOW_FALLBACK_FORWARD_NUMBER or "").strip()
    try:
        if not fallback:
            logger.error(
                "FLOW FALLBACK: no FLOW_FALLBACK_FORWARD_NUMBER configured; hanging up "
                "(linkedid=%s)", lid,
            )
            await ari.hangup(channel_id)
            return
        logger.error("FLOW FALLBACK: forwarding linkedid=%s to %s", lid, fallback)
        await ari.answer(channel_id)
        result = await ari.dial_number(channel_id, fallback, caller_id=None, timeout_s=25.0)
        logger.error("FLOW FALLBACK: forward of linkedid=%s ended with '%s'", lid, result)
    except Exception:  # noqa: BLE001 - the safety net itself must never raise
        logger.exception("FLOW FALLBACK: forward failed (linkedid=%s)", lid)
    finally:
        try:
            await ari.hangup(channel_id)
        except Exception:  # noqa: BLE001
            logger.exception("FLOW FALLBACK: final hangup failed (linkedid=%s)", lid)

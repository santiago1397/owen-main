"""`retell` engine — Retell holds the conversation, OWEN keeps the call (RETELL-PLAN, 2026-10-06).

The seam is the one every engine satisfies: `run(spec, ctx) -> AgentResult{port, data}`,
blocking for the whole conversation, called by the flow's `ai_agent` node through
`flows/runtime.run_agent_on_call`. What differs is where the voice lives. owen_voice receives
the caller's audio over an externalMedia leg; this engine DIALS Retell over SIP and bridges
the caller to it, so OWEN still owns everything around the conversation: the flow, the
recording (of the bridge, here), the fallback, Listen and Take over.

## The steps, and what each failure does

    1. spend cap reached             -> failed (+ the day's alert)
    2. no RETELL_API_KEY / no agent id / no ARI
                                      -> failed, with a logged sentence, NO request made
    3. the caller brief (CRM, 1.2 s)  -> on any failure the agent is told the caller is
                                         unknown; never a reason to fail the call
    4. register-phone-call            -> failed on any error (integrations/retell/client.py)
    5. the call id is persisted       -> failed if it cannot be: without it no webhook,
                                         function or take-over could find this call
    6. originate the SIP leg, wait for answer (RETELL_CONNECT_TIMEOUT_SECONDS) -> failed
    7. bridge caller + Retell, record the bridge -> failed if the bridge is refused
    8. wait for: Retell hangs up (end_call) | the caller hangs up (default) | a function asks
       to leave (transfer / end_call, after a short grace so the agent's last sentence is
       heard) | a supervisor took over (taken_over)

`failed` routes to the flow's fallback (voicemail) — decision 17. A caller never hears dead
air because Retell is down, unconfigured or over budget.

## What comes back later

The transcript, Retell's summary and the real cost are not known when the call ends here;
Retell posts them to `/api/retell/webhook` (integrations/retell/webhook.py), which stores them
against the same call and reports them to the CRM under the same dedupe key.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.agents.session import AgentCallContext, AgentResult, AgentSpec

logger = logging.getLogger("agents.retell")

ENGINE_NAME = "retell"

# How often the wait loop asks the registry whether a function call wants the agent to stop.
POLL_SECONDS = 0.5
# After a function asks to transfer or end, how long the Retell leg stays up so the agent's
# "I'm putting you through now" is heard rather than cut off mid-word.
EXIT_GRACE_SECONDS = 1.5
# A backstop above any real call when the agent declares no max_call_seconds.
DEFAULT_MAX_CALL_SECONDS = 3600
# The caller-brief budget: the CRM gets 0.8 s inside OWEN's adapter; this covers the hop.
BRIEF_TIMEOUT_SECONDS = 1.2

_LEG_GONE = frozenset({"ChannelHangupRequest", "StasisEnd", "ChannelDestroyed"})
# The leg id prefix. It MUST start with the flow-dial marker so the ARI consumer never treats
# the Retell leg's StasisStart as a new inbound call (providers/asterisk.is_flow_dial_leg).
LEG_PREFIX_SUFFIX = "retell-"


def _failed(reason: str, lid: str) -> AgentResult:
    logger.warning("retell: %s; taking the failed port (linkedid=%s)", reason, lid)
    return AgentResult(port="failed", data={"engine": ENGINE_NAME, "failed_reason": reason})


class RetellVoiceAgentSession:
    """One call's conversation, held by Retell."""

    name = ENGINE_NAME

    async def run(self, spec: AgentSpec, ctx: AgentCallContext) -> AgentResult:
        from app.agents import spend
        from app.core.config import settings

        lid = ctx.linkedid
        if not (settings.RETELL_API_KEY or "").strip():
            return _failed("RETELL_API_KEY is not set, so Retell is off", lid)
        retell_agent_id = str((spec.config or {}).get("retell_agent_id") or "").strip()
        if not retell_agent_id:
            return _failed("the agent version has no retell_agent_id", lid)
        if ctx.ari is None:
            return _failed("no ARI client on this call", lid)
        if await spend.over_cap():
            await spend.check_alert()
            return _failed("the daily AI spend cap is reached", lid)

        variables = await _dynamic_variables(spec, ctx)

        from app.integrations.retell import client as retell_client
        from app.integrations.retell import registry

        pinned = (spec.config or {}).get("retell_agent_version")
        body = retell_client.register_body(
            agent_id=retell_agent_id,
            from_number=ctx.caller_number or "",
            to_number=getattr(ctx, "dialed_number", None) or "",
            metadata={"linkedid": lid, "owen_agent": spec.agent_name or spec.agent_id,
                      "owen_version": spec.version_number if spec.version_number is not None
                      else spec.version_id},
            variables=variables,
            agent_version=int(pinned) if isinstance(pinned, int) and not isinstance(pinned, bool)
            else None,
        )
        try:
            call = await retell_client.register_phone_call(body)
        except retell_client.RetellError as exc:
            return _failed(str(exc), lid)
        call_id = str(call.get("call_id"))

        reg = registry.current()
        try:
            await reg.create(
                retell_call_id=call_id, linkedid=lid, status=registry.LIVE,
                retell_agent_id=retell_agent_id, agent_name=spec.agent_name or "",
                agent_version_id=spec.version_id, caller_number=ctx.caller_number,
                dialed_number=getattr(ctx, "dialed_number", None),
                call_channel_id=ctx.channel_id,
            )
        except Exception:  # noqa: BLE001 - unmappable call: no webhook/function could find it
            logger.exception("retell: recording call %s failed (linkedid=%s)", call_id, lid)
            return _failed("the Retell call could not be recorded here", lid)

        logger.info("retell: registered call %s for linkedid=%s", call_id, lid)
        return await _connect_and_wait(spec, ctx, call_id, reg)


async def _connect_and_wait(spec: AgentSpec, ctx: AgentCallContext, call_id: str,
                            reg) -> AgentResult:
    """Steps 6-8. Whatever happens, the registry row ends `ended`, and the Retell leg and the
    bridge are torn down — unless a supervisor owns the call now, in which case nothing here
    may touch it (telephony/ownership.py)."""
    from app.core.config import settings
    from app.flows import dtmf
    from app.providers.asterisk import FLOW_DIAL_CHANNEL_PREFIX
    from app.telephony import ownership

    ari, lid, caller_chan = ctx.ari, ctx.linkedid, ctx.channel_id
    out_id = f"{FLOW_DIAL_CHANNEL_PREFIX}{LEG_PREFIX_SUFFIX}{uuid.uuid4().hex}"
    endpoint = (f"PJSIP/{settings.RETELL_SIP_ENDPOINT}/"
                f"sip:{call_id}@{settings.RETELL_SIP_HOST}")
    queue = dtmf.watch(out_id, caller_chan)
    bridge_id = None
    leg_gone = False
    rec_name = f"{lid}-agent-1"
    loop = asyncio.get_running_loop()
    bridged_at = None
    result = AgentResult(port="failed", data={"engine": ENGINE_NAME})
    try:
        answer = await ari.originate_sip_leg(
            queue, caller_chan, out_id, endpoint,
            timeout_s=float(settings.RETELL_CONNECT_TIMEOUT_SECONDS or 10),
        )
        if answer != "answered":
            # Not marked gone: a leg our own timer gave up on may still be ringing, and the
            # DELETE in the finally is a harmless 404 for one that is already destroyed.
            result = _failed(f"the Retell SIP leg did not answer ({answer})", lid)
            return result
        bridge_id = await ari.create_bridge()
        if not bridge_id or not await ari.add_to_bridge(bridge_id, caller_chan, out_id):
            result = _failed("the caller could not be bridged to Retell", lid)
            return result
        # The BRIDGE, never a channel: a channel recording started first makes ARI refuse the
        # addChannel above (the 409 the dial path already paid for).
        await ari.record_bridge(bridge_id, rec_name)
        bridged_at = loop.time()
        try:
            await reg.update(call_id, retell_channel_id=out_id, bridge_id=bridge_id)
        except Exception:  # noqa: BLE001 - only take-over needs these; the call goes on
            logger.exception("retell: storing channels for %s failed", call_id)

        guard = spec.guardrails if isinstance(spec.guardrails, dict) else {}
        try:
            max_s = float(guard.get("max_call_seconds") or DEFAULT_MAX_CALL_SECONDS)
        except (TypeError, ValueError):
            max_s = DEFAULT_MAX_CALL_SECONDS
        how, exit_req = await _wait(queue, reg, call_id, caller_chan, out_id, max_s)
        leg_gone = how == "retell_gone"

        if ownership.is_owned(lid):
            port, data = "taken_over", {}
        elif exit_req is not None:
            port, data = exit_req[0], dict(exit_req[1])
            await asyncio.sleep(EXIT_GRACE_SECONDS)
        elif how == "caller_gone":
            port, data = "default", {"ended_by": "caller"}
        elif how == "max_duration":
            port, data = "end_call", {"ended_by": "max_duration"}
        else:
            # Retell hung up its own leg: the agent ended the conversation (its native
            # end-call), or Retell dropped. Which one is known only from the webhook's
            # `disconnection_reason`, which the CRM receives too.
            port, data = "end_call", {"ended_by": "retell"}
        result = AgentResult(port=port, data=data)
        return result
    except Exception:  # noqa: BLE001 - never dead air: the node takes `failed`
        logger.exception("retell: call %s failed (linkedid=%s)", call_id, lid)
        result = AgentResult(port="failed", data={"engine": ENGINE_NAME})
        return result
    finally:
        dtmf.unwatch(queue, out_id, caller_chan)
        if not ownership.is_owned(lid):
            if not leg_gone:
                await _quiet(ari.hangup(out_id))
            if bridge_id:
                await _quiet(ari.destroy_bridge(bridge_id))
        snap = None
        try:
            snap = await reg.finish(call_id)
        except Exception:  # noqa: BLE001
            logger.exception("retell: marking %s ended failed", call_id)
        _decorate(result, snap or {}, call_id,
                  rec_name if bridge_id else None,
                  (loop.time() - bridged_at) if bridged_at is not None else None)


async def _quiet(coro) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001 - teardown is best-effort
        logger.debug("retell: teardown step failed", exc_info=True)


def _decorate(result: AgentResult, snap: dict, call_id: str, rec_name: str | None,
              duration_s: float | None) -> None:
    """Put what the runtime and the CRM report need on the result, in the keys they already
    read: `captured` (call_captures), `destination` (the transfer allowlist), `recording_name`
    (the agent recording), `duration_s`, and `ai_call_extra` (C4's engine fields)."""
    data = dict(result.data or {})
    data["engine"] = ENGINE_NAME
    data["retell_call_id"] = call_id
    if isinstance(snap.get("captured"), dict) and snap["captured"]:
        data["captured"] = dict(snap["captured"])
    if result.port == "transfer" and data.get("destination") is None and \
            isinstance(snap.get("exit_data"), dict):
        data["destination"] = snap["exit_data"].get("destination")
    if rec_name:
        data["recording_name"] = rec_name
    if duration_s is not None and duration_s > 0:
        data["duration_s"] = round(duration_s, 1)
    extra = {"engine": ENGINE_NAME, "retell_call_id": call_id}
    if snap.get("requests"):
        extra["requests"] = list(snap["requests"])
    data["ai_call_extra"] = extra
    result.data = data


async def _wait(queue: asyncio.Queue, reg, call_id: str, caller_chan: str, out_id: str,
                max_s: float) -> tuple[str, tuple[str, dict] | None]:
    """Block until the conversation ends. Returns `(how, exit_request)`; `how` is one of
    retell_gone | caller_gone | exit | max_duration."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    next_poll = 0.0
    while True:
        if loop.time() - started > max_s:
            return "max_duration", None
        try:
            event = await asyncio.wait_for(queue.get(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            event = None
        if isinstance(event, dict) and event.get("type") in _LEG_GONE:
            ch = event.get("channel") if isinstance(event.get("channel"), dict) else {}
            cid = str(ch.get("id") or "")
            if cid == out_id:
                return "retell_gone", await _safe_exit_request(reg, call_id)
            if cid == caller_chan:
                return "caller_gone", None
        if loop.time() >= next_poll:
            next_poll = loop.time() + POLL_SECONDS
            req = await _safe_exit_request(reg, call_id)
            if req is not None:
                return "exit", req


async def _safe_exit_request(reg, call_id: str):
    try:
        return await reg.exit_request(call_id)
    except Exception:  # noqa: BLE001 - a DB blip must not end a live conversation
        logger.debug("retell: exit request poll failed", exc_info=True)
        return None


async def _dynamic_variables(spec: AgentSpec, ctx: AgentCallContext) -> dict:
    """C2's five variables. The brief comes from the CRM only when the agent's
    `context_provider` is `crm_link`; anything that goes wrong means "unknown caller"."""
    from app.integrations.retell.brief import render_variables

    caller = ctx.caller_number or ""
    dialed = getattr(ctx, "dialed_number", None) or ""
    answer = None
    try:
        answer = await _fetch_brief(spec, ctx)
    except Exception:  # noqa: BLE001 - context is an enhancement; never fail a call for it
        logger.exception("retell: fetching the caller brief failed (linkedid=%s)", ctx.linkedid)
    return render_variables(answer, caller_number=caller, dialed_number=dialed)


async def _fetch_brief(spec: AgentSpec, ctx: AgentCallContext) -> dict | None:
    """The CRM's raw C2 answer, through OWEN's own adapter (the worker cannot reach the CRM:
    it is on callmon-net only — integrations/crm/client.py)."""
    import httpx

    from app.core.config import settings
    from app.integrations.crm import config as crm_config

    cfg = (spec.config or {}).get("context_provider")
    if not isinstance(cfg, dict) or str(cfg.get("kind") or "").lower() != "crm_link":
        return None
    if not ctx.caller_number:
        return None
    link = crm_config.current()
    refusal = link.delivery_refusal() or ("" if link.base_url else "no CRM_LINK_BASE_URL")
    if refusal or not settings.AGENT_RUNTIME_KEY:
        logger.warning("retell: caller brief unavailable (%s); the agent is told nothing",
                       refusal or "AGENT_RUNTIME_KEY is not set")
        return None
    url = f"{settings.OWEN_INTERNAL_URL.rstrip('/')}/api/agent-runtime/crm-link/brief"
    async with httpx.AsyncClient(timeout=BRIEF_TIMEOUT_SECONDS) as client:
        resp = await client.post(url, headers={"X-OWEN-Key": settings.AGENT_RUNTIME_KEY}, json={
            "caller_number": ctx.caller_number,
            "dialed_number": getattr(ctx, "dialed_number", None) or "",
            "linkedid": ctx.linkedid,
            "agent_name": spec.agent_name or "",
        })
    if resp.status_code >= 400:
        logger.warning("retell: caller brief refused (%s)", resp.status_code)
        return None
    data = resp.json()
    return data.get("answer") if isinstance(data, dict) else None

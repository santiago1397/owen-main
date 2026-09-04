"""Supervisor monitoring: LISTEN to a live agent call, and SEIZE it (AI_AGENT_SPEC D4/D5).

Two modes, and deliberately only two:

  LISTEN    ARI snoop with `spy=both, whisper=none`. The operator hears the caller and the
            agent; neither hears the operator.
  TAKE OVER The operator's own leg joins the call, the agent's external-media leg is ejected,
            and the call is marked HUMAN-OWNED so no automated path can touch it again.

Whisper/coach — a third classic mode — is deliberately absent. It lets a supervisor talk
privately to one side, which is meaningful when coaching a human and meaningless against an
LLM: you cannot voice-coach a model mid-turn.

Structure mirrors `AsteriskAriClient.run_outbound_call`, and for the same hard-won reasons:
watch the legs BEFORE originating (or the StasisStart arrives before the originate returns),
and never bridge a channel that has not yet entered Stasis (ARI answers 422/409 and the legs
silently never join).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.core.calllog import clog
from app.flows import dtmf
from app.telephony import ownership
from app.telephony.credentials import operator_dial_endpoint

logger = logging.getLogger("telephony.monitor")

# How long the operator's softphone rings before we give up setting up a monitor session.
OPERATOR_ANSWER_TIMEOUT_S = 30.0

_LEG_GONE = frozenset({"StasisEnd", "ChannelDestroyed", "ChannelHangupRequest"})


async def _wait_for_stasis(queue: asyncio.Queue, channel_id: str, timeout_s: float) -> bool:
    """Wait until `channel_id` enters Stasis (answered), or it dies / we time out."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        try:
            event = await asyncio.wait_for(queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            return False
        if not isinstance(event, dict):
            continue
        ch = event.get("channel") if isinstance(event.get("channel"), dict) else {}
        if str(ch.get("id") or "") != channel_id:
            continue
        etype = event.get("type")
        if etype == "StasisStart":
            return True
        if etype in _LEG_GONE:
            return False


async def start_listen(
    ari, *, operator_id: str, target_channel_id: str, linkedid: str,
    operator_channel_id: str,
) -> dict:
    """Ring the operator and bridge them to a SNOOP of the target call.

    The snoop is one-way by construction (`whisper="none"`), so the operator is inaudible to
    both the caller and the agent. Returns the ids needed to later take over or stop.
    """
    queue = dtmf.watch(operator_channel_id)
    clog(logger, "monitor.listen.begin", linkedid=linkedid, operator=operator_id,
         channel=target_channel_id)
    try:
        if not await ari._originate_with_id(
            operator_channel_id, operator_dial_endpoint(operator_id),
            caller_id=f"Monitor {linkedid}"[:64],
        ):
            return {"ok": False, "reason": "operator_originate_failed"}
        if not await _wait_for_stasis(queue, operator_channel_id, OPERATOR_ANSWER_TIMEOUT_S):
            await ari.hangup(operator_channel_id)
            return {"ok": False, "reason": "operator_no_answer"}

        snoop_id = await ari.snoop(target_channel_id, spy="both", whisper="none")
        if not snoop_id:
            await ari.hangup(operator_channel_id)
            return {"ok": False, "reason": "snoop_failed"}

        bridge_id = await ari.create_bridge()
        if not bridge_id or not await ari.add_to_bridge(
            bridge_id, snoop_id, operator_channel_id
        ):
            await ari.hangup(snoop_id)
            await ari.hangup(operator_channel_id)
            return {"ok": False, "reason": "bridge_failed"}

        clog(logger, "monitor.listen.ok", linkedid=linkedid, operator=operator_id,
             snoop=snoop_id, bridge=bridge_id)
        return {
            "ok": True,
            "operator_channel": operator_channel_id,
            "snoop_channel": snoop_id,
            "monitor_bridge": bridge_id,
        }
    finally:
        dtmf.unwatch(queue, operator_channel_id)


async def take_over(
    ari, *, operator_id: str, linkedid: str, target_channel_id: str,
    operator_channel_id: str, call_bridge_id: Optional[str] = None,
    snoop_channel_id: Optional[str] = None, monitor_bridge_id: Optional[str] = None,
    agent_channel_id: Optional[str] = None,
) -> dict:
    """Hand the call to the human, permanently.

    ORDER MATTERS and is the contract:
      1. CLAIM OWNERSHIP FIRST. Everything after this point is a mutation of a live call, and
         until the claim lands the flow interpreter is still entitled to hang up or play at
         that channel. Claiming first is what makes the window safe rather than narrow.
      2. Drop the snoop (the operator is about to hear the call directly, not a copy).
      3. Eject the agent's media leg, so it stops listening and speaking.
      4. Put the operator into the caller's bridge.

    The caller's channel is never hung up: they are mid-conversation and about to be handed a
    human.
    """
    if not ownership.claim(
        linkedid, operator_id,
        channels=[c for c in (target_channel_id, operator_channel_id, agent_channel_id) if c],
        reason="takeover",
    ):
        return {"ok": False, "reason": "already_owned", "owner": ownership.owner_of(linkedid)}

    clog(logger, "monitor.takeover", linkedid=linkedid, operator=operator_id,
         channel=target_channel_id)

    # 2. The snoop copy is redundant once the operator is in the real bridge, and leaving it
    #    would double every word the operator hears.
    if snoop_channel_id:
        await ari.hangup(snoop_channel_id)
    if monitor_bridge_id:
        await ari.destroy_bridge(monitor_bridge_id)

    # 3. The agent stops when its media leg goes away: the AudioSocket connection closes and
    #    owen-voice ends the session. (It is also told explicitly by the API layer, so the
    #    session reports `taken_over` rather than a plain hangup.)
    if agent_channel_id:
        await ari.hangup(agent_channel_id)

    # 4. Operator joins the caller. If the call has no bridge yet (the agent was attached
    #    directly), make one and put both in it.
    bridge_id = call_bridge_id
    if not bridge_id:
        bridge_id = await ari.create_bridge()
        if bridge_id:
            await ari.add_to_bridge(bridge_id, target_channel_id)
    if not bridge_id:
        # Ownership stays claimed: the call is in a half-moved state and the LAST thing it
        # needs is an automated path deciding to "clean up" by hanging up on the caller.
        logger.error("monitor: takeover could not obtain a bridge for %s", linkedid)
        return {"ok": False, "reason": "bridge_failed"}

    if not await ari.add_to_bridge(bridge_id, operator_channel_id):
        logger.error("monitor: takeover could not bridge the operator for %s", linkedid)
        return {"ok": False, "reason": "operator_bridge_failed"}

    ownership.add_channel(linkedid, operator_channel_id)
    clog(logger, "monitor.takeover.ok", linkedid=linkedid, operator=operator_id,
         bridge=bridge_id)
    return {"ok": True, "bridge": bridge_id, "owner": operator_id}


async def stop_listen(
    ari, *, snoop_channel_id: Optional[str], operator_channel_id: Optional[str],
    monitor_bridge_id: Optional[str],
) -> None:
    """End a listen session. Never touches the monitored call itself."""
    if snoop_channel_id:
        await ari.hangup(snoop_channel_id)
    if operator_channel_id:
        await ari.hangup(operator_channel_id)
    if monitor_bridge_id:
        await ari.destroy_bridge(monitor_bridge_id)

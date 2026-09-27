"""What happens when a call arrives on a CRM-BOUND DID.

This is the CRM-link analogue of `flows/runtime.py::_handle_unassigned`, and it is
deliberately the same shape, step for step, because that sequence is what works today on a
live phone system:

    answer -> recording-consent notice (played to completion) -> ring -> bridge + record
           -> on no answer, voicemail

The only thing that differs is the middle: the ring group also rings up to two PSTN numbers
(see `ring.py`), and each phase is reported to the CRM (see `push.py`).

## What this handler will NOT do

  * It never runs for a DID with a flow assigned. A flow is an explicit operator intent and
    `DEFAULT_CALL_HANDLING_SPEC` already establishes that an assigned flow overrides the
    built-in default; a CRM binding is a different default, not a higher authority. A DID
    that is both bound and flow-assigned logs a WARNING once per call and runs its flow.
  * It never runs when `CRM_LINK_ENABLED` is false, or when the DID has no enabled
    `crm_links` row. `hook.handle_bound_inbound` returns False before this is reached.

## The AI-agent seam (wired in phase 3, 2026-09-25 — OFF by default)

`_ai_agent_seam` sits between "nobody answered" and "take a voicemail". With
`CRM_LINK_AGENT_ANSWERS` false (the default) it returns before touching anything and the
call takes the voicemail exactly as it always has. With it true, the call is handed to the
AI agent of the bound number's CAMPAIGN, through the same runtime a flow's `ai_agent` node
uses (`flows/runtime.py::run_agent_on_call`). Its docstring says what happens in each case.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from app.core.calllog import clog
from app.core.config import settings
from app.integrations.crm import config as crm_config
from app.integrations.crm import push, ring
from app.integrations.crm.binding import CrmBinding
from app.integrations.crm.events import (PHASE_ANSWERED, PHASE_ENDED, PHASE_STARTED)

logger = logging.getLogger("integrations.crm.handler")


async def _resolve_operators(ari, binding: CrmBinding) -> tuple[list[str], bool]:
    """The softphone half of the ring group: `(operators, already_endpoints)`.

    An empty `operator_ids` means "every operator whose softphone is CURRENTLY registered",
    which is the availability rule the platform already uses (DEFAULT_CALL_HANDLING_SPEC
    decision #1: availability IS SIP registration, because the InCallBar toggle is what
    registers and unregisters the endpoint). An explicit list is dialled as given — if the
    owner names the CRM desk, it rings whether or not presence says it is there, and the leg
    simply never answers if it is not.
    """
    if not binding.ring_operators:
        return [], False
    if binding.operator_ids:
        return list(binding.operator_ids), False
    try:
        return list(await ari.available_operators()), True
    except Exception:  # noqa: BLE001 - presence is best-effort; PSTN legs still ring
        logger.exception("crm-link: available_operators failed for %s", binding.phone_number)
        return [], True


@dataclass
class AgentHandoff:
    """What happened when the CRM line handed an unanswered call to an agent.

    `claimed` — the agent dealt with the call; the handler takes no voicemail.
    `hang_up` — and the channel is still ours to end (False after a transfer or a take-over,
                when somebody else owns it now).
    `extra`   — the agent's part of the call for the handler's ONE `ended` event (transcript,
                `ai_call`, `dedupe_key`), so the CRM files one call row, not two.
    """

    claimed: bool
    port: str = ""
    hang_up: bool = False
    extra: dict = field(default_factory=dict)


async def _ai_agent_seam(
    ari, channel_id: str, lid: str, binding: CrmBinding, caller_number: str,
    *, dialed: str = "", notice_played: bool = False,
) -> Optional[AgentHandoff]:
    """The AI agent answers a CRM-line call that nobody picked up — ONLY when switched on.

    Returns None when no agent ran (the handler then takes the voicemail, as it always did),
    or an `AgentHandoff` when one did.

    OFF (`CRM_LINK_AGENT_ANSWERS` false, the default): returns None immediately, before any
    database read. Nobody answers -> voicemail, byte for byte the behaviour before phase 3.

    ON: after the ring group has failed (no answer, busy, everyone declined, or nothing to
    ring), in this order —
      1. the recording-consent notice must already have played on this call (the handler
         plays it right after answering, before the ring). If it did not — no
         INBOUND_CONSENT_MEDIA configured — NO agent: an agent's call is
         recorded, Florida is all-party consent, and the notice is not skippable on this path.
      2. the agent is the CAMPAIGN's: the bound number's `campaigns.agent_id`, the same
         pointer every campaign number uses (step 3 of `_agent_id_for_node`). Chosen over a
         per-binding agent column because the binding already carries the number's
         `campaign_id`, a second pointer could disagree with the first, and it keeps ONE rule
         for "which agent answers this DID". No campaign, an inactive campaign, a campaign
         with no agent, or an agent with no ACTIVE version -> None -> voicemail.
      3. the agent runs through `run_agent_on_call`, the flow node's own runtime: version
         pinned on the call, captures/transcript/cost stored, the transfer allowlist
         honoured, the campaign's facts passed as context. Then by its exit port:
           * `failed` (capacity, spend cap, service down, no version) -> None -> voicemail;
           * `transfer` with no destination it may use -> NOT claimed: there is no flow edge
             here and the people were already rung, so the caller leaves a voicemail — and
             the agent's transcript still rides the `ended` event;
           * `transferred` / `taken_over` -> claimed, and the channel is left alone;
           * anything else (`default`, `end_call`) -> claimed, and the handler hangs up.
    Every failure inside returns None: this seam can only ever ADD an agent in front of the
    voicemail, never take the voicemail away.
    """
    if not bool(getattr(settings, "CRM_LINK_AGENT_ANSWERS", False)):
        return None
    if not notice_played:
        logger.warning(
            "crm-link: CRM_LINK_AGENT_ANSWERS is on but the recording-consent notice did not "
            "play on this call (INBOUND_CONSENT_MEDIA is unset); taking the "
            "voicemail instead of an agent (linkedid=%s)", lid,
        )
        return None
    try:
        # Lazy: the flow runtime imports this package lazily too, and a CRM link that is off
        # must not pay for the agent stack.
        from app.agents.crm_call import report_extra
        from app.db import SessionLocal
        from app.flows import runtime as flow_runtime
        from app.flows.interpreter import STAND_DOWN_PORTS
        from app.integrations.crm import push as crm_push
        from app.services.ingestion import _get_or_create_provider

        async with SessionLocal() as db:
            campaign = await flow_runtime._campaign_facts(db, binding.campaign_id)
            if campaign is None or not campaign.agent_id:
                clog(logger, "crmlink.agent.none", linkedid=lid,
                     campaign=binding.campaign_id, next="voicemail")
                return None
            provider = await _get_or_create_provider(db, flow_runtime.PROVIDER_NAME)
            provider_id = provider.id
            await db.commit()
            owen_call_id, _rec, _trans = await crm_push.call_artifacts(db, lid)

        clog(logger, "crmlink.agent.start", linkedid=lid, agent=campaign.agent_id,
             campaign=campaign.campaign_id)
        run = await flow_runtime.run_agent_on_call(
            ari=ari, channel_id=channel_id, lid=lid,
            dialed=dialed or binding.phone_number, caller_number=caller_number,
            provider_id=provider_id, agent_id=campaign.agent_id, campaign=campaign,
            report_to_crm=False,
        )
    except Exception:  # noqa: BLE001 - the voicemail is always still there
        logger.exception("crm-link: handing the call to an agent failed (linkedid=%s)", lid)
        return None

    clog(logger, "crmlink.agent.end", linkedid=lid, port=run.port)
    if run.port == "failed":
        return None
    extra = report_extra(agent_name=run.agent_name, version=run.version, outcome=run.port,
                         data=run.data, campaign=campaign.name,
                         owen_call_id=owen_call_id or "")
    if run.port == "transfer":
        return AgentHandoff(claimed=False, port=run.port, extra=extra)
    return AgentHandoff(claimed=True, port=run.port,
                        hang_up=run.port not in STAND_DOWN_PORTS, extra=extra)


async def handle_bound_inbound(
    ari, channel_id: str, lid: str, dialed: str, caller_number: str, binding: CrmBinding,
) -> None:
    """Run the hybrid ring group for a CRM-bound DID.

    Best-effort throughout, like every other handler on this path: it never raises into the
    consumer, and every exit ends with the caller hearing something — a person, a voicemail
    greeting, or at worst a clean hangup. Never dead air.
    """
    cfg = crm_config.current()
    started_at = time.monotonic()
    outcome = "failed"
    winner_dest: Optional[str] = None
    winner_kind: Optional[str] = None
    notice_played = False
    agent_extra: dict = {}

    clog(logger, "crmlink.call.begin", linkedid=lid, channel=channel_id, dialed=dialed,
         caller=caller_number or None, link=binding.link_id)

    # Phase 1 of 3. Queued, never awaited for its delivery: the caller is on the line.
    await push.report_call_phase(
        phase=PHASE_STARTED, linkedid=lid, binding=binding,
        caller_number=caller_number, dialed_number=dialed,
    )

    try:
        await ari.answer(channel_id)

        # Recording-consent notice, played to completion. Florida is all-party consent
        # (ARCHITECTURE.md #17) and the bridge below IS recorded, so this is not optional
        # decoration. Identical to `_handle_unassigned`, including the setting it reads.
        consent = (settings.INBOUND_CONSENT_MEDIA or "").strip()
        if consent:
            clog(logger, "crmlink.consent", linkedid=lid, channel=channel_id)
            await ari.play_and_wait(channel_id, consent)
            # The agent hand-off below requires this. `play_and_wait` reports nothing back
            # (an unplayable prompt returns quietly), so "played" means "configured and
            # played to completion or its cap" — the same standard the recorded bridge uses.
            notice_played = True

        operators, are_endpoints = await _resolve_operators(ari, binding)
        allowed_pstn, refused_pstn = cfg.filter_pstn(binding.pstn_numbers)
        for number, reason in refused_pstn:
            # Loud, because a ring group that silently shrinks is only ever noticed by a
            # customer who did not get called back.
            logger.warning(
                "crm-link: NOT ringing %s for DID %s (linkedid=%s) — %s",
                number, dialed, lid, reason,
            )

        legs = ring.build_legs(
            dialed=dialed,
            caller_number=caller_number,
            bound_did=binding.phone_number or dialed,
            operators=operators,
            pstn_numbers=allowed_pstn,
            operators_are_endpoints=are_endpoints,
        )
        clog(logger, "crmlink.ring.plan", linkedid=lid, operators=len(operators),
             pstn=len(allowed_pstn), refused=len(refused_pstn), legs=len(legs))

        if legs:
            record_name = f"{lid}-crmlink-1" if settings.INBOUND_RECORDING_ENABLED else None
            await ari.ring_start(channel_id)
            try:
                result = await ring.hybrid_ring_and_bridge(
                    ari, channel_id, legs,
                    timeout_s=float(binding.ring_timeout_seconds),
                    record_name=record_name,
                )
            finally:
                await ari.ring_stop(channel_id)

            if result.answered:
                winner_dest = result.winning_destination
                winner_kind = result.winning_kind
                outcome = "answered"
                # The `answered` event carries the winning destination — the fact a hybrid
                # group exists to produce, and the one the CRM cannot derive on its own.
                await push.report_call_phase(
                    phase=PHASE_ANSWERED, linkedid=lid, binding=binding,
                    caller_number=caller_number, dialed_number=dialed,
                    outcome="answered",
                    winning_destination=winner_dest, winning_kind=winner_kind,
                )
                clog(logger, "crmlink.call.answered", linkedid=lid,
                     destination=winner_dest, kind=winner_kind)
                await ari.hangup(channel_id)  # the bridge ended; end the call
                return
            outcome = result.port  # "noanswer" | "failed"
            clog(logger, "crmlink.call.no_answer", linkedid=lid, result=result.port,
                 reason=result.reason, next="voicemail")
        else:
            outcome = "noanswer"
            logger.warning(
                "crm-link: DID %s is bound but has NO ring destinations "
                "(operators=%d, allowed PSTN=%d); going straight to voicemail (linkedid=%s)",
                dialed, len(operators), len(allowed_pstn), lid,
            )

        # --- nobody answered -------------------------------------------------------------
        handoff = await _ai_agent_seam(ari, channel_id, lid, binding, caller_number,
                                       dialed=dialed, notice_played=notice_played)
        if handoff is not None:
            agent_extra = dict(handoff.extra or {})
            if handoff.claimed:
                outcome = "agent"
                if handoff.hang_up:
                    await ari.hangup(channel_id)
                return

        clog(logger, "crmlink.voicemail", linkedid=lid, channel=channel_id)
        await ari.voicemail(
            channel_id,
            greeting=settings.VOICEMAIL_GREETING,
            name=f"{lid}-vm-1",
            max_duration_s=float(settings.VOICEMAIL_MAX_DURATION_SECONDS),
            max_silence_s=float(settings.VOICEMAIL_MAX_SILENCE_SECONDS),
        )
        outcome = "voicemail"
    except Exception:  # noqa: BLE001 - must never raise into the consumer
        logger.exception("crm-link handling failed for DID %s (linkedid=%s)", dialed, lid)
        outcome = "failed"
        try:
            await ari.hangup(channel_id)
        except Exception:  # noqa: BLE001
            logger.exception("crm-link: final hangup failed (linkedid=%s)", lid)
    finally:
        # Phase 3 of 3, on EVERY exit including the failure path. This is the event the CRM
        # derives its call report from, so it must exist even for a call that went wrong —
        # a missing row and a failed call look identical from the CRM, and only one of them
        # is something somebody should follow up.
        duration = int(max(0.0, time.monotonic() - started_at))
        clog(logger, "crmlink.call.end", linkedid=lid, outcome=outcome,
             duration_s=duration, destination=winner_dest)
        await push.report_call_phase(
            phase=PHASE_ENDED, linkedid=lid, binding=binding,
            caller_number=caller_number, dialed_number=dialed,
            outcome=outcome, duration_seconds=duration,
            winning_destination=winner_dest, winning_kind=winner_kind,
            # The agent's transcript / ai_call / dedupe_key when one answered (phase 3):
            # ONE ended event per call, so the CRM files one call row, not two.
            extra=agent_extra or None,
        )

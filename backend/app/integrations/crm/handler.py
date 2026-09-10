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

## The AI-agent seam

`_ai_agent_seam` is where an `ai_agent` node drops in on a later phase, between "nobody
answered" and "take a voicemail". It is a function that returns False, with the exact
information an agent hand-off needs already in scope. It is NOT wired: the agent platform
is live, and connecting it to a real business line is its own change with its own review.
"""

from __future__ import annotations

import logging
import time
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


async def _ai_agent_seam(
    ari, channel_id: str, lid: str, binding: CrmBinding, caller_number: str,
) -> bool:
    """SEAM — NOT WIRED. Where the AI agent answers a call nobody picked up.

    Return True to claim the call (this handler then does nothing further and does not take
    a voicemail); return False to fall through to voicemail. It returns False today.

    To wire it, this is the shape the rest of the platform already expects: resolve an agent
    (directly, or through an `AgentSlot` as `flows/runtime.py::_agent_id_for_node` does),
    pin its version onto the call, run a `VoiceAgentSession` with an `AgentCallContext`
    carrying `channel_id` / `linkedid` / `caller_number`, persist its captures and
    transcript, and map its exit port. All of that machinery exists and is live; what does
    not exist is a decision that a real customer calling a real roofing business should
    reach it, and that decision is not this module's to make.

    The arguments are taken (and named) now so wiring it later is an edit to this function
    and nothing else.
    """
    del ari, channel_id, lid, binding, caller_number  # documented seam; intentionally unused
    return False


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
        if await _ai_agent_seam(ari, channel_id, lid, binding, caller_number):
            outcome = "agent"
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
        )

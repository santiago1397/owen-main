"""The hybrid ring group — the one genuinely new piece of call logic in this module.

Rings CRM browser softphones (`PJSIP/operator-<slug>`) and up to two PSTN numbers
(`PJSIP/<e164>@<trunk>`) **at the same time**, bridges the caller to whichever answers
first, tears down every other leg, and records the bridge exactly as the existing paths do.

## Why this is not `AsteriskAriClient.ring_and_bridge`

`ring_and_bridge` takes a list of endpoint strings and would, on the face of it, accept a
trunk endpoint among the softphones. It is still the wrong function, for a reason that is
about telephony rather than tidiness: **it applies ONE `callerId` to every leg.**

  * An operator leg wants `"<dialed DID>" <caller number>` — the browser popup reads the
    caller out of the SIP `From` user and the dialed DID out of its display name
    (DEFAULT_CALL_HANDLING_SPEC, "How the popup learns to what number"). That is
    caller-ID passthrough, and it is correct there because the leg terminates on our own
    Asterisk.
  * A PSTN leg must NOT present the inbound caller's number. Sending a number we do not own
    out over the BulkVS trunk is caller-ID spoofing: the carrier may reject the INVITE
    outright, and where it does not, the mobile that rings shows a number that is not ours
    and a callback goes to the customer instead of to the business. A PSTN leg has to
    present the BOUND DID.

One caller-ID cannot be both. So this function carries the caller-ID **per leg**, which is
the difference between "ring_and_bridge with a different list" and a new function.

Everything else is deliberately identical to `ring_and_bridge`, including the parts that
look incidental and are not:
  * every leg is originated INTO our own Stasis app, marked with `FLOW_DIAL_APP_ARG` and a
    `FLOW_DIAL_CHANNEL_PREFIX` channel id, so `asterisk_consumer` never mistakes an answered
    leg for a fresh inbound call and starts a second flow on it;
  * `originator=<caller channel>` collapses every leg onto the caller's Linkedid, so the
    whole thing stays ONE `calls` row;
  * the BRIDGE is recorded, never a channel — a channel recording started before the
    addChannel makes ARI reject the bridge with a 409 and leaves both parties on dead air
    (the live 2026-08-05 failure that `tests/test_dial_record_bridge.py` guards);
  * the `finally` always detaches the watcher, destroys the bridge and deletes every leg.

`_await_first_answer` is REUSED from `AsteriskAriClient` rather than copied. It carries
live-call fixes — StasisStart vs `ChannelStateChange`->Up, caller-hangup-while-ringing,
per-leg `ChannelDestroyed` pruning — and a fork of it would freeze that knowledge at
today's date and quietly miss the next fix. It is a private method; the coupling is
deliberate and is recorded in `.qa/state/crmlink-done`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app.core.calllog import clog
from app.core.config import settings
from app.flows import dtmf
from app.providers.asterisk import FLOW_DIAL_APP_ARG, FLOW_DIAL_CHANNEL_PREFIX
from app.telephony.credentials import operator_dial_endpoint

logger = logging.getLogger("integrations.crm.ring")

KIND_OPERATOR = "operator"
KIND_PSTN = "pstn"


@dataclass
class RingLeg:
    """One destination in the group, with the ARI endpoint and caller-ID it needs."""

    kind: str                    # KIND_OPERATOR | KIND_PSTN
    destination: str             # operator id (email) or E.164
    endpoint: str                # PJSIP/operator-<slug>  |  PJSIP/<e164>@<trunk>
    caller_id: Optional[str]
    channel_id: str = ""         # pre-assigned so the watcher exists before the leg does


@dataclass
class RingResult:
    """What the group did. Everything here is asserted on by the tests and logged."""

    port: str = "noanswer"                          # answered | noanswer | failed
    winner: Optional[RingLeg] = None
    originated: list[str] = field(default_factory=list)     # channel ids ARI accepted
    failed_to_originate: list[str] = field(default_factory=list)
    torn_down: list[str] = field(default_factory=list)      # channel ids explicitly hung up
    bridge_id: Optional[str] = None
    reason: Optional[str] = None

    @property
    def answered(self) -> bool:
        return self.port == "answered"

    @property
    def winning_destination(self) -> Optional[str]:
        return self.winner.destination if self.winner else None

    @property
    def winning_kind(self) -> Optional[str]:
        return self.winner.kind if self.winner else None


def operator_caller_id(dialed: str, caller_number: str) -> str:
    """Caller-ID for a softphone leg. Byte-for-byte the string `_handle_unassigned` builds,
    because the browser popup parses it: display name = the DID that was dialed, URI user =
    who is calling. Changing the shape here would silently break the popup's "to what
    number" line without breaking the call, which is the worst kind of change."""
    return f"{dialed} <{caller_number}>" if caller_number else dialed


def pstn_caller_id(bound_did: str, caller_number: str) -> str:
    """Caller-ID for a PSTN leg: the BOUND DID as the number (see the module docstring), the
    inbound caller's number as the display name.

    The display name is a free bet — most PSTN carriers drop it — but where it does survive,
    the mobile shows both who is calling and, on callback, still reaches the business."""
    did = str(bound_did or "").strip()
    if caller_number:
        return f"{caller_number} <{did}>"
    return did


def build_legs(
    *,
    dialed: str,
    caller_number: str,
    bound_did: str,
    operators: list[str],
    pstn_numbers: list[str],
    trunk_name: Optional[str] = None,
    operators_are_endpoints: bool = False,
) -> list[RingLeg]:
    """Assemble the group. PURE — no ARI, no database, no network.

    `operators_are_endpoints` distinguishes the two shapes an operator arrives in:
    `available_operators()` already returns `PJSIP/operator-<slug>` strings, while a
    binding's `operator_ids` are raw identities that still need `operator_dial_endpoint`.

    Allowlisting is NOT done here. `pstn_numbers` must already have passed
    `config.CrmLinkSettings.filter_pstn` — keeping the guard at the caller means there is
    exactly one place a destination is authorised, and it is not buried in a builder.
    """
    trunk = trunk_name or settings.BULKVS_TRUNK_NAME
    legs: list[RingLeg] = []
    for op in operators or []:
        op = str(op or "").strip()
        if not op:
            continue
        endpoint = op if operators_are_endpoints else operator_dial_endpoint(op)
        legs.append(RingLeg(
            kind=KIND_OPERATOR,
            destination=op,
            endpoint=endpoint,
            caller_id=operator_caller_id(dialed, caller_number),
        ))
    for num in pstn_numbers or []:
        num = str(num or "").strip()
        if not num:
            continue
        legs.append(RingLeg(
            kind=KIND_PSTN,
            destination=num,
            endpoint=f"PJSIP/{num}@{trunk}",
            caller_id=pstn_caller_id(bound_did, caller_number),
        ))
    for leg in legs:
        leg.channel_id = f"{FLOW_DIAL_CHANNEL_PREFIX}{uuid.uuid4().hex}"
    return legs


async def hybrid_ring_and_bridge(
    ari, channel_id: str, legs: list[RingLeg], *, timeout_s: float,
    record_name: str | None = None,
) -> RingResult:
    """Ring every leg at once; bridge the caller to the first to answer; drop the rest.

    Returns a `RingResult` rather than the bare port string `ring_and_bridge` returns,
    because the whole reason for a hybrid group is knowing WHICH destination answered — the
    CRM timeline entry says so, and an operator debugging "why did my cell not ring?" needs
    to see which legs were even originated.

    Best-effort throughout: like every other call-path function in this codebase it never
    raises into the caller. A failure returns `port="failed"` and the handler routes the
    caller to voicemail rather than leaving dead air.
    """
    result = RingResult()
    legs = [leg for leg in (legs or []) if leg.endpoint]
    if not legs:
        result.reason = "no ring destinations"
        return result

    timeout_s = max(1.0, float(timeout_s or 25))
    watch_ids = [leg.channel_id for leg in legs] + [channel_id]
    queue = dtmf.watch(*watch_ids)
    bridge_id: str | None = None
    by_channel = {leg.channel_id: leg for leg in legs}

    clog(logger, "crmlink.ring.start", channel=channel_id, legs=len(legs),
         operators=sum(1 for leg in legs if leg.kind == KIND_OPERATOR),
         pstn=sum(1 for leg in legs if leg.kind == KIND_PSTN),
         timeout_s=int(timeout_s), record=bool(record_name))
    try:
        for leg in legs:
            params = {
                "endpoint": leg.endpoint,
                "app": settings.ARI_APP,
                # Both flow-dial markers. Without them an ANSWERED leg's StasisStart looks
                # like a fresh inbound call to the consumer, which would run the default
                # handler on it and play the voicemail greeting at whoever picked up.
                "appArgs": FLOW_DIAL_APP_ARG,
                "channelId": leg.channel_id,
                "originator": channel_id,
                # ARI's own no-answer timer. Belt and braces with the awaiter's deadline:
                # this one tears the leg down with a real Q.850 cause.
                "timeout": str(int(timeout_s)),
            }
            if leg.caller_id:
                params["callerId"] = str(leg.caller_id)
            created = await ari._post_json("/ari/channels", params=params)
            if isinstance(created, dict) and created.get("id"):
                result.originated.append(leg.channel_id)
            else:
                # One dead destination must not cancel the group — the whole point of
                # ringing three phones is that any one of them can be unreachable.
                result.failed_to_originate.append(leg.destination)
                clog(logger, "crmlink.ring.leg_failed", channel=channel_id,
                     kind=leg.kind, destination=leg.destination, level=logging.WARNING)

        if not result.originated:
            result.port = "failed"
            result.reason = "no leg could be originated"
            clog(logger, "crmlink.ring.result", channel=channel_id, result="failed",
                 reason=result.reason, level=logging.WARNING)
            return result

        answered_id = await ari._await_first_answer(
            queue, channel_id, set(result.originated), timeout_s
        )
        if answered_id is None:
            result.port = "noanswer"
            clog(logger, "crmlink.ring.result", channel=channel_id, result="noanswer",
                 legs=len(result.originated))
            return result

        winner = by_channel.get(answered_id)
        result.winner = winner

        # THE LOSERS GO FIRST. Hang up every other still-ringing leg before bridging, so a
        # second phone cannot be answered into a call that is already being connected — and
        # so nobody's cell keeps ringing after the desk picked up.
        for leg in legs:
            if leg.channel_id != answered_id:
                await ari._delete(f"/ari/channels/{leg.channel_id}")
                result.torn_down.append(leg.channel_id)

        clog(logger, "crmlink.ring.answered", channel=channel_id, answered=answered_id,
             kind=getattr(winner, "kind", None),
             destination=getattr(winner, "destination", None))

        bridge_id = await ari.create_bridge()
        if not bridge_id:
            result.port = "failed"
            result.reason = "no_bridge"
            clog(logger, "crmlink.ring.result", channel=channel_id, result="failed",
                 reason="no_bridge", level=logging.WARNING)
            return result
        result.bridge_id = bridge_id
        if not await ari.add_to_bridge(bridge_id, channel_id, answered_id):
            # Reporting "answered" on a rejected bridge is what turned a 409 into 25s of
            # dead air on a live call. Take the failed port so the handler can go to
            # voicemail and the caller hears something.
            result.port = "failed"
            result.reason = "bridge_rejected"
            clog(logger, "crmlink.ring.result", channel=channel_id, result="failed",
                 reason="bridge_rejected", level=logging.WARNING)
            return result
        if record_name:
            await ari.record_bridge(bridge_id, record_name)

        clog(logger, "crmlink.ring.bridged", channel=channel_id, bridge=bridge_id,
             destination=getattr(winner, "destination", None))
        # Block until either bridged leg leaves, exactly as the existing dial paths do.
        ended = await ari._await_bridge_end(queue, channel_id, answered_id)
        clog(logger, "crmlink.ring.ended", channel=channel_id, bridge=bridge_id,
             ended_by=(ended or {}).get("dial_ended_by"),
             talk_ms=(ended or {}).get("dial_talk_ms"))
        result.port = "answered"
        return result
    except Exception:  # noqa: BLE001 - a ring failure falls through to voicemail, never crashes
        logger.exception("crm-link hybrid ring failed for channel %s", channel_id)
        result.port = "failed"
        result.reason = "exception"
        return result
    finally:
        dtmf.unwatch(queue, *watch_ids)
        if bridge_id:
            await ari.destroy_bridge(bridge_id)
        # Drop anything still up: the winner after the bridge ended, and any leg the
        # loser-teardown above did not reach because we bailed out early. DELETE on an
        # already-gone channel is a harmless 404.
        for leg in legs:
            if leg.channel_id not in result.torn_down:
                await ari._delete(f"/ari/channels/{leg.channel_id}")
                result.torn_down.append(leg.channel_id)

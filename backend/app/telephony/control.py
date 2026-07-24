"""Server-side ARI control ORCHESTRATION for the operator softphone (Ticket 13).

The locked control split: SIP.js drives ONLY its own leg (INVITE/answer/BYE); ALL
bridge / hold / blind-transfer go through the BACKEND over ARI — NEVER browser->ARI. The
FastAPI endpoints in app/api/telephony.py are thin auth wrappers over these functions.

PURE (stdlib only): every function takes an injected `ari` implementing `AriControlOps`
(the concrete impl is AsteriskAriClient in app/providers/asterisk_client.py; tests pass a
FAKE), so the orchestration — which ARI ops fire, in what order — is unit-testable with no
httpx. Endpoint RESOLUTION (transfer target kind -> a PJSIP/Local resource) is pure and
tested too.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol

from app.core.calllog import clog
from app.telephony.credentials import operator_dial_endpoint

logger = logging.getLogger("telephony.control")

# Blind-transfer target kinds (v1 — attended transfer is out of scope).
TRANSFER_KINDS = frozenset({"did", "operator", "ai_agent"})


class AriControlOps(Protocol):
    """The extra ARI control ops the backend drives for the softphone (beyond the
    interpreter's AriControl). Implemented by AsteriskAriClient; faked in tests."""

    async def hold(self, channel_id: str) -> None: ...
    async def unhold(self, channel_id: str) -> None: ...
    async def create_bridge(self) -> Optional[str]: ...
    async def add_to_bridge(self, bridge_id: str, *channel_ids: str) -> None: ...
    async def destroy_bridge(self, bridge_id: str) -> None: ...
    async def blind_transfer(self, channel_id: str, endpoint: str) -> None: ...


class OutboundAriOps(Protocol):
    """ARI ops the backend drives to place a manual operator OUTBOUND call (Ticket 14).
    Implemented by AsteriskAriClient; faked in tests. `originate_*` return the new channel's
    id (or None on failure). `originator` links the callee leg onto the operator leg's Linkedid
    so the whole call collapses onto ONE `calls` row (the ticket-04/05 projection invariant)."""

    async def originate_operator(
        self, operator_id: str, *, caller_id: Optional[str] = None,
        variables: Optional[dict] = None,
    ) -> Optional[str]: ...
    async def originate_number(
        self, number: str, *, caller_id: Optional[str] = None, trunk_name: Optional[str] = None,
        originator: Optional[str] = None, variables: Optional[dict] = None,
    ) -> Optional[str]: ...
    async def play(self, channel_id: str, media: str) -> None: ...
    async def create_bridge(self) -> Optional[str]: ...
    async def add_to_bridge(self, bridge_id: str, *channel_ids: str) -> None: ...
    async def record_bridge(self, bridge_id: str, name: str) -> None: ...


def resolve_transfer_endpoint(
    kind: str, target: str, *, trunk_name: str, agent_context: str = "owen-agents"
) -> Optional[str]:
    """Map a blind-transfer (kind, target) to the ARI endpoint string to redirect to, or
    None if the request is malformed (the endpoint then rejects it — never a silent misdial).

      - "did":      an external number over the BulkVS trunk  -> PJSIP/<number>@<trunk>
      - "operator": another operator's browser leg            -> PJSIP/operator-<slug>
      - "ai_agent": the AI-agent runtime (a Local channel into its Stasis/dialplan context)
                    -> Local/<agent-id>@<agent_context>
    """
    if not target:
        return None
    kind = (kind or "").strip()
    if kind == "did":
        return f"PJSIP/{target}@{trunk_name}"
    if kind == "operator":
        return operator_dial_endpoint(target)
    if kind == "ai_agent":
        return f"Local/{target}@{agent_context}"
    return None


async def hold(ari: AriControlOps, channel_id: str) -> None:
    await ari.hold(channel_id)


async def unhold(ari: AriControlOps, channel_id: str) -> None:
    await ari.unhold(channel_id)


async def bridge(ari: AriControlOps, channel_a: str, channel_b: str) -> Optional[str]:
    """Create a mixing bridge and add both legs (the operator's browser channel + the
    caller channel under the same Linkedid). Returns the bridge id, or None on failure."""
    bridge_id = await ari.create_bridge()
    if not bridge_id:
        return None
    await ari.add_to_bridge(bridge_id, channel_a, channel_b)
    return bridge_id


async def blind_transfer(
    ari: AriControlOps, channel_id: str, kind: str, target: str, *, trunk_name: str
) -> Optional[str]:
    """Blind-transfer `channel_id` to (kind, target). Resolves the endpoint, then redirects
    the channel. Returns the endpoint transferred to, or None if the target was malformed."""
    endpoint = resolve_transfer_endpoint(kind, target, trunk_name=trunk_name)
    if endpoint is None:
        return None
    await ari.blind_transfer(channel_id, endpoint)
    return endpoint


# Channel variable the OUTBOUND legs are stamped with so the ticket-04/05 ARI projection
# tags the resulting `calls` row `direction='outbound'` (the router reads it — otherwise it
# defaults to 'inbound'). The owned-DID from-number rides X_OWEN_FROM so campaign attribution
# keys on the from-number for outbound (the reverse of the inbound to-number match).
DIRECTION_VAR = "X_OWEN_DIRECTION"
FROM_VAR = "X_OWEN_FROM"


async def place_outbound_call(
    ari: OutboundAriOps,
    *,
    operator_id: str,
    callee_number: str,
    from_number: str,
    trunk_name: str,
    consent_media: Optional[str] = None,
    record: bool = True,
    operator_channel_id: Optional[str] = None,
) -> dict:
    """Orchestrate a manual operator outbound call (Ticket 14), server-side over ARI.

    Sequence (the order is the contract — asserted in tests):
      1. Obtain the OPERATOR leg. Default: originate the operator's own WebRTC endpoint
         (`PJSIP/operator-<slug>`) into Stasis so their SIP.js rings/answers — this yields a
         server-known channel id with NO WS correlation. (If the caller already knows the
         operator's live Stasis channel — e.g. from an operator-initiated INVITE — it passes
         `operator_channel_id` and we skip the originate.) The operator (entry) leg carries the
         direction + from-number channel vars so the projection tags the row outbound.
      2. Originate the CALLEE over the BulkVS trunk with the owned-DID caller-ID, linked onto
         the operator leg's Linkedid via `originator` (=> ONE `calls` row for both legs).
      3. PRE-BRIDGE consent notice: play the recording-consent prompt to the CALLEE *before*
         the operator is bridged in (the outbound analogue of the inbound entry consent).
      4. Bridge operator + callee.
      5. Start recording on the bridge (recording ON by default).

    Returns a result dict: {ok, reason?, bridge_id?, operator_channel?, callee_channel?}. A
    failure at any originate/bridge step short-circuits with ok=False + a reason (best-effort;
    the endpoint surfaces it rather than hanging on a half-set-up call)."""
    op_vars = {DIRECTION_VAR: "outbound", FROM_VAR: from_number}
    clog(logger, "outbound.begin", operator=operator_id, to=callee_number, from_number=from_number)

    if operator_channel_id:
        op_channel: Optional[str] = operator_channel_id
    else:
        # caller_id shown on the operator's own softphone = who they're calling.
        op_channel = await ari.originate_operator(
            operator_id, caller_id=callee_number, variables=op_vars
        )
        if not op_channel:
            clog(logger, "outbound.fail", operator=operator_id, reason="operator_originate_failed",
                 level=logging.WARNING)
            return {"ok": False, "reason": "operator_originate_failed"}

    callee_channel = await ari.originate_number(
        callee_number,
        caller_id=from_number,
        trunk_name=trunk_name,
        originator=op_channel,
        variables=op_vars,
    )
    if not callee_channel:
        clog(logger, "outbound.fail", channel=op_channel, reason="callee_originate_failed",
             level=logging.WARNING)
        return {"ok": False, "reason": "callee_originate_failed",
                "operator_channel": op_channel}

    # Consent to the callee BEFORE bridging the operator in.
    if consent_media:
        await ari.play(callee_channel, consent_media)

    bridge_id = await ari.create_bridge()
    if not bridge_id:
        clog(logger, "outbound.fail", channel=op_channel, reason="bridge_failed",
             level=logging.WARNING)
        return {"ok": False, "reason": "bridge_failed",
                "operator_channel": op_channel, "callee_channel": callee_channel}
    await ari.add_to_bridge(bridge_id, op_channel, callee_channel)
    clog(logger, "outbound.bridged", channel=op_channel, callee=callee_channel, bridge=bridge_id)

    if record:
        # Name the recording `{linkedid}-...` so the ticket-05 recording pipeline attaches it
        # to this call's row (recording_linkedid splits on the first '-'). For the default
        # originate path the operator (entry) channel id IS the Linkedid.
        await ari.record_bridge(bridge_id, f"{op_channel}-outbound")

    clog(logger, "outbound.ok", channel=op_channel, callee=callee_channel, bridge=bridge_id,
         recording=record)
    return {
        "ok": True,
        "bridge_id": bridge_id,
        "operator_channel": op_channel,
        "callee_channel": callee_channel,
    }

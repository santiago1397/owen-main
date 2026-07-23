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

from typing import Optional, Protocol

from app.telephony.credentials import operator_dial_endpoint

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

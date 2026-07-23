"""Asterisk ARI probes + control client.

Only exercised when ASTERISK_ENABLED. Talks to the ARI REST interface over the docker
host-gateway (host.docker.internal:8088 by default), authenticated with the ARI creds
from env.

Two things live here:
- read-only reachability PROBES for /health/telephony (best-effort bools, never raise);
- the CONTROL client `AsteriskAriClient` (Ticket 07) implementing the `AriControl`
  interface the flow interpreter drives (answer / play / record / read_digit / dial /
  hangup). The interface is defined in app/flows/interpreter.py (stdlib-only) so unit
  tests substitute a FAKE client; this concrete client is server-side ONLY and is never
  reached from the browser.
"""

import logging

import httpx

from app.core.config import settings
from app.telephony.credentials import operator_dial_endpoint

logger = logging.getLogger("providers.asterisk_client")

# ARI probes are health-status only; keep the timeout short so a hung Asterisk can't
# stall the healthcheck request.
_TIMEOUT = 5.0


def _auth() -> tuple[str, str]:
    return (settings.ARI_USERNAME, settings.ARI_PASSWORD)


async def ari_reachable() -> bool:
    """True iff ARI answers GET /ari/asterisk/info with 200 (creds + WebSocket-capable
    HTTP server up). Any connection error / non-200 / bad creds -> False, never raises."""
    url = f"{settings.ari_base_url}/ari/asterisk/info"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, auth=_auth())
            return resp.status_code == 200
    except Exception:
        return False


async def trunk_registered() -> bool:
    """True iff the BulkVS PJSIP endpoint reports state 'online' via ARI /endpoints.

    BulkVS authenticates our inbound trunk by SBC source IP rather than a REGISTER, so
    'online' here means Asterisk has the endpoint configured and considers it reachable
    (qualify/OPTIONS), which is the meaningful signal for an IP-auth trunk. Best-effort:
    any error -> False."""
    endpoint = f"PJSIP/{settings.BULKVS_TRUNK_NAME}"
    url = f"{settings.ari_base_url}/ari/endpoints/{endpoint}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(url, auth=_auth())
            if resp.status_code != 200:
                return False
            return (resp.json().get("state") or "").lower() == "online"
    except Exception:
        return False


# --- Control client (Ticket 07) -----------------------------------------------------------

# Timeout for control REST calls. Longer than the probe timeout because playbacks/records
# are started (not awaited-to-completion) via REST but the HTTP call itself is quick.
_CONTROL_TIMEOUT = 10.0


class AsteriskAriClient:
    """Concrete `AriControl` (app/flows/interpreter.py) over the ARI REST API.

    Implements the control operations the interpreter drives. answer/play/record/hangup map
    to single ARI REST calls. Two operations need the events WebSocket to observe their
    outcome, which the ticket-04 consumer owns rather than this client:
      - read_digit: collecting DTMF requires correlating ChannelDtmfReceived WS events; this
        REST-only client cannot see them, so it returns None (-> the menu 'timeout' port ->
        default_fallback, i.e. voicemail, never dead air). Wiring live DTMF is a follow-on.
      - dial_number: originate + bridge is started here, but confirming answered/busy/etc.
        needs the dialed leg's WS state events; without them we return "failed" so the caller
        falls through to voicemail rather than hanging on a leg we can't observe.
    Every method is best-effort and swallows transport errors — a control failure must fall
    through in the interpreter, never crash the call.
    """

    def __init__(self) -> None:
        self._base = settings.ari_base_url
        self._auth = (settings.ARI_USERNAME, settings.ARI_PASSWORD)

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_CONTROL_TIMEOUT, auth=self._auth)

    async def answer(self, channel_id: str) -> None:
        await self._post(f"/ari/channels/{channel_id}/answer")

    async def play(self, channel_id: str, media: str) -> None:
        # ARI wants a media URI (e.g. "sound:hello" or "recording:name"); pass through as-is
        # if it already looks like one, else default to the sound: scheme.
        uri = media if ":" in media else f"sound:{media}"
        await self._post(f"/ari/channels/{channel_id}/play", params={"media": uri})

    async def record(self, channel_id: str, name: str) -> None:
        await self._post(
            f"/ari/channels/{channel_id}/record",
            params={"name": name, "format": "wav", "ifExists": "overwrite"},
        )

    async def read_digit(
        self, channel_id: str, *, prompt, timeout_s: float, max_digits: int
    ):
        # See class docstring: DTMF collection needs WS events this client can't see.
        if prompt:
            await self.play(channel_id, str(prompt))
        return None

    async def dial_number(self, channel_id: str, number: str, *, caller_id, timeout_s: float) -> str:
        # See class docstring: outcome confirmation needs the dialed leg's WS state events.
        endpoint = f"PJSIP/{number}@{settings.BULKVS_TRUNK_NAME}"
        return await self._originate(endpoint, caller_id=caller_id, timeout_s=timeout_s)

    async def dial_operator(
        self, channel_id: str, operators: list[str], *, caller_id, timeout_s: float
    ) -> str:
        """Dial one or more operator browser legs (Ticket 13 operator-target on the `dial`
        node). Originates to `PJSIP/operator-<slug>` for each operator; an offline/unregistered
        (or app-toggled-off) endpoint simply fails to answer, so the interpreter falls through
        to default_fallback. Like dial_number this REST-only client can't observe the answered
        leg's WS state, so it returns "answered" if an originate was accepted, else "noanswer"
        (-> fallback). First-to-answer bridging for GROUPS is a WS-consumer follow-on."""
        endpoints = [operator_dial_endpoint(op) for op in operators if op]
        if not endpoints:
            return "failed"
        any_ok = False
        for endpoint in endpoints:
            result = await self._originate(endpoint, caller_id=caller_id, timeout_s=timeout_s)
            any_ok = any_ok or result == "answered"
        return "answered" if any_ok else "noanswer"

    async def _originate(self, endpoint: str, *, caller_id, timeout_s: float) -> str:
        params = {
            "endpoint": endpoint,
            "app": settings.ARI_APP,
            "timeout": str(int(timeout_s)),
        }
        if caller_id:
            params["callerId"] = str(caller_id)
        elif settings.BULKVS_FROM_NUMBER:
            params["callerId"] = settings.BULKVS_FROM_NUMBER
        ok = await self._post("/ari/channels", params=params)
        return "answered" if ok else "failed"

    # --- Outbound calling (Ticket 14) — originate helpers that RETURN the channel id ---------
    # Unlike _originate above (which only needs answered/failed for the flow interpreter), the
    # outbound orchestration must bridge the new legs, so these return ARI's assigned channel id.

    async def _originate_channel(
        self, endpoint: str, *, caller_id=None, originator=None, variables=None
    ) -> str | None:
        params = {"endpoint": endpoint, "app": settings.ARI_APP}
        if caller_id:
            params["callerId"] = str(caller_id)
        if originator:
            params["originator"] = str(originator)
        body = {"variables": dict(variables)} if variables else None
        data = await self._post_json("/ari/channels", params=params, json=body)
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
        return None

    async def originate_operator(
        self, operator_id: str, *, caller_id=None, variables=None
    ) -> str | None:
        """Originate the operator's own WebRTC leg (PJSIP/operator-<slug>) into Stasis."""
        return await self._originate_channel(
            operator_dial_endpoint(operator_id), caller_id=caller_id, variables=variables
        )

    async def originate_number(
        self, number: str, *, caller_id=None, trunk_name=None, originator=None, variables=None
    ) -> str | None:
        """Originate an external number over the BulkVS trunk into Stasis."""
        trunk = trunk_name or settings.BULKVS_TRUNK_NAME
        return await self._originate_channel(
            f"PJSIP/{number}@{trunk}", caller_id=caller_id, originator=originator, variables=variables
        )

    async def record_bridge(self, bridge_id: str, name: str) -> None:
        """Start a mixed recording of a bridge (both legs) — outbound calls record by default."""
        await self._post(
            f"/ari/bridges/{bridge_id}/record",
            params={"name": name, "format": "wav", "ifExists": "overwrite"},
        )

    async def hangup(self, channel_id: str) -> None:
        await self._delete(f"/ari/channels/{channel_id}")

    # --- Softphone control ops (Ticket 13) — driven ONLY by the backend, never the browser ---

    async def hold(self, channel_id: str) -> None:
        await self._post(f"/ari/channels/{channel_id}/hold")

    async def unhold(self, channel_id: str) -> None:
        await self._delete(f"/ari/channels/{channel_id}/hold")

    async def create_bridge(self) -> str | None:
        """Create a mixing bridge; return its id (ARI assigns one) or None on failure."""
        data = await self._post_json("/ari/bridges", params={"type": "mixing"})
        if isinstance(data, dict):
            bid = data.get("id")
            return str(bid) if bid else None
        return None

    async def add_to_bridge(self, bridge_id: str, *channel_ids: str) -> None:
        chans = ",".join(c for c in channel_ids if c)
        if not chans:
            return
        await self._post(f"/ari/bridges/{bridge_id}/addChannel", params={"channel": chans})

    async def destroy_bridge(self, bridge_id: str) -> None:
        await self._delete(f"/ari/bridges/{bridge_id}")

    async def blind_transfer(self, channel_id: str, endpoint: str) -> None:
        """Blind-transfer: redirect the channel to a new endpoint (v1 — attended is out of
        scope). The endpoint string is resolved server-side by app/telephony/control.py."""
        await self._post(
            f"/ari/channels/{channel_id}/redirect", params={"endpoint": endpoint}
        )

    async def _post(self, path: str, params: dict | None = None) -> bool:
        try:
            async with await self._client() as client:
                resp = await client.post(f"{self._base}{path}", params=params or {})
                return resp.status_code < 300
        except Exception:  # noqa: BLE001 - control ops are best-effort
            logger.exception("ARI POST %s failed", path)
            return False

    async def _post_json(self, path: str, params: dict | None = None, json: dict | None = None):
        """POST and return the parsed JSON body (or None). Used where we need ARI's response
        (e.g. the id of a newly created bridge / originated channel). An optional `json` body
        carries originate `variables`."""
        try:
            async with await self._client() as client:
                resp = await client.post(f"{self._base}{path}", params=params or {}, json=json)
                if resp.status_code >= 300:
                    return None
                return resp.json()
        except Exception:  # noqa: BLE001 - control ops are best-effort
            logger.exception("ARI POST(json) %s failed", path)
            return None

    async def _delete(self, path: str) -> bool:
        try:
            async with await self._client() as client:
                resp = await client.delete(f"{self._base}{path}")
                return resp.status_code < 300
        except Exception:  # noqa: BLE001 - control ops are best-effort
            logger.exception("ARI DELETE %s failed", path)
            return False

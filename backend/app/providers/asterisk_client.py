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

    async def hangup(self, channel_id: str) -> None:
        await self._delete(f"/ari/channels/{channel_id}")

    async def _post(self, path: str, params: dict | None = None) -> bool:
        try:
            async with await self._client() as client:
                resp = await client.post(f"{self._base}{path}", params=params or {})
                return resp.status_code < 300
        except Exception:  # noqa: BLE001 - control ops are best-effort
            logger.exception("ARI POST %s failed", path)
            return False

    async def _delete(self, path: str) -> bool:
        try:
            async with await self._client() as client:
                resp = await client.delete(f"{self._base}{path}")
                return resp.status_code < 300
        except Exception:  # noqa: BLE001 - control ops are best-effort
            logger.exception("ARI DELETE %s failed", path)
            return False

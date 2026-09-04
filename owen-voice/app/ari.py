"""Thin ARI client — only the operations the echo spike needs.

Intentionally NOT a copy of the backend's 1,146-line AsteriskAriClient. This service owns a
different concern (media), and duplicating that client would create two places where ARI
semantics live. If owen-voice ever needs the full surface, the right move is to extract a
shared package, not to fork.

Every call is best-effort and logs rather than raising into the event loop: an ARI failure
must degrade the media path, never take down the process handling other calls.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger("voice.ari")

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class AriClient:
    def __init__(self) -> None:
        self._auth = (settings.ARI_USERNAME, settings.ARI_PASSWORD)
        self._base = settings.ari_base

    async def _request(self, method: str, path: str, **kw) -> Optional[Any]:
        url = f"{self._base}{path}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.request(method, url, auth=self._auth, **kw)
        except Exception as exc:  # noqa: BLE001 - transport failure is not fatal to the loop
            logger.warning("ari %s %s failed: %r", method, path, exc)
            return None
        if r.status_code >= 400:
            # ARI's error bodies are short and specific ("Channel not in Stasis application",
            # "Channel currently recording"). Logging the body verbatim is what turns a 409
            # into a five-second diagnosis instead of an afternoon.
            logger.warning("ari %s %s -> %s %s", method, path, r.status_code, r.text[:300])
            return None
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    # --- channels --------------------------------------------------------------------------

    async def answer(self, channel_id: str) -> None:
        await self._request("POST", f"/channels/{channel_id}/answer")

    async def hangup(self, channel_id: str) -> None:
        await self._request("DELETE", f"/channels/{channel_id}")

    async def originate_to_stasis(
        self, endpoint: str, *, caller_id: Optional[str] = None,
        channel_id: Optional[str] = None, timeout_s: int = 45,
    ) -> Optional[str]:
        """Originate a channel STRAIGHT into our Stasis app.

        The `app` parameter is the whole trick behind this spike needing no Asterisk config:
        a channel originated with `app` set enters that Stasis application directly, bypassing
        the dialplan entirely. So we can prove the media path without touching extensions.conf
        — which the asterisk/README warns is rendered from templates and has already been
        silently corrupted once by a bare envsubst.
        """
        params: dict[str, Any] = {
            "endpoint": endpoint,
            "app": settings.ARI_APP,
            "timeout": timeout_s,
        }
        if caller_id:
            params["callerId"] = caller_id
        if channel_id:
            params["channelId"] = channel_id
        data = await self._request("POST", "/channels", params=params)
        if data is None:
            return None
        return data.get("id") if isinstance(data, dict) else None

    async def create_external_media(
        self, *, session_uuid: str, channel_id: Optional[str] = None
    ) -> Optional[str]:
        """Create the external-media channel that streams this call's audio to us.

        `data` carries our session UUID, which Asterisk sends back as the FIRST AudioSocket
        frame — the only link between an inbound TCP connection and the call it belongs to.
        `connection_type=client` means Asterisk dials out to us, so we are a plain TCP server.
        """
        params: dict[str, Any] = {
            "app": settings.ARI_APP,
            "external_host": settings.AUDIOSOCKET_ADVERTISE,
            "format": settings.MEDIA_FORMAT,
            "encapsulation": "audiosocket",
            "transport": "tcp",
            "connection_type": "client",
            "direction": "both",
            "data": session_uuid,
        }
        if channel_id:
            params["channelId"] = channel_id
        data = await self._request("POST", "/channels/externalMedia", params=params)
        if data is None:
            return None
        return data.get("id") if isinstance(data, dict) else None

    # --- bridges ---------------------------------------------------------------------------

    async def create_bridge(self, bridge_type: str = "mixing") -> Optional[str]:
        data = await self._request("POST", "/bridges", params={"type": bridge_type})
        if data is None:
            return None
        return data.get("id") if isinstance(data, dict) else None

    async def add_to_bridge(self, bridge_id: str, *channel_ids: str) -> bool:
        ok = await self._request(
            "POST", f"/bridges/{bridge_id}/addChannel",
            params={"channel": ",".join(channel_ids)},
        )
        return ok is not None

    async def destroy_bridge(self, bridge_id: str) -> None:
        await self._request("DELETE", f"/bridges/{bridge_id}")

    # --- diagnostics -------------------------------------------------------------------------

    async def ping(self) -> bool:
        """Is ARI reachable and are our credentials good? Used by /health so a misconfigured
        password surfaces as a red healthcheck rather than as a silent failure on a real call."""
        return await self._request("GET", "/asterisk/info") is not None

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

import asyncio
import logging
import uuid

import httpx

from app.core.config import settings
from app.flows import dtmf
from app.providers.asterisk import FLOW_DIAL_APP_ARG, FLOW_DIAL_CHANNEL_PREFIX
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

# Ticket 15.3 dial+bridge tuning. The grace is added on top of the node's ring timeout so
# ARI's own originate `timeout` (which tears the leg down with a no-answer cause) usually
# fires FIRST and we route on its ChannelDestroyed rather than our timer.
_DIAL_ANSWER_GRACE_S = 2.0
# Hard ceiling on a bridged dial (belt-and-braces: if the WS drops mid-bridge and we miss
# both legs' hangup events, the correlation task must still end, not live forever).
_DIAL_BRIDGE_MAX_S = 4 * 3600.0
# Events that mean a leg LEFT the call (hangup requested / left Stasis / destroyed).
_LEG_GONE_EVENTS = frozenset({"ChannelHangupRequest", "StasisEnd", "ChannelDestroyed"})
# Q.850 hangup cause -> dial-node port for the outbound leg's ChannelDestroyed while
# ringing. 17=user busy; 18/19/21=no response/no answer/rejected. Unlisted -> "failed".
_DIAL_CAUSE_PORTS = {17: "busy", 18: "noanswer", 19: "noanswer", 21: "noanswer"}


class AsteriskAriClient:
    """Concrete `AriControl` (app/flows/interpreter.py) over the ARI REST API.

    Implements the control operations the interpreter drives. answer/play/record/hangup map
    to single ARI REST calls. Two operations additionally correlate events from the ARI
    WebSocket via the app/flows/dtmf registries the ticket-04 consumer feeds (Ticket 15):
      - read_digit awaits the channel's registered digit queue (ChannelDtmfReceived) with
        the node's timeout; with no queue registered it returns None (-> 'timeout' port ->
        default_fallback, never dead air).
      - dial_number originates the outbound leg into our own Stasis app and watches its
        lifecycle events to confirm answered/busy/noanswer, bridges on answer, and blocks
        until either leg leaves.
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
        uri = await self._resolve_media(str(media))
        if not uri:
            return  # unplayable prompt (TTS failed): skip playback, flow continues
        await self._post(f"/ari/channels/{channel_id}/play", params={"media": uri})

    async def _resolve_media(self, media: str) -> str | None:
        """Prompt string -> ARI media URI (Ticket 15.2).

        Already a media URI ("sound:...", "recording:...", ...) -> pass through untouched.
        Otherwise the string is PROMPT TEXT: resolve it through the TTS cache (synthesizing
        lazily on a miss) to an absolute-path `sound:` URI (path without extension — the
        recordings volume is host-shared so Asterisk reads the same path, see
        asterisk/README.md). If TTS fails, a single WORD is still tried as a legacy bare
        sound name; multi-word text is skipped (None) so the flow continues without audio.
        """
        from app.services.tts import is_media_uri, resolve_prompt

        if is_media_uri(media):
            return media
        try:
            path = await resolve_prompt(media)
            if path:
                return f"sound:{path}"
        except Exception:  # noqa: BLE001 - TTS is best-effort; never break playback flow
            logger.exception("ARI play: TTS resolve failed")
        if " " not in media:
            return f"sound:{media}"  # legacy convenience: a bare provisioned sound name
        return None

    async def record(self, channel_id: str, name: str) -> None:
        await self._post(
            f"/ari/channels/{channel_id}/record",
            params={"name": name, "format": "wav", "ifExists": "overwrite"},
        )

    async def read_digit(
        self, channel_id: str, *, prompt, timeout_s: float, max_digits: int
    ):
        """Collect DTMF (Ticket 15.1): play the prompt, then await the channel's digit
        queue (fed by the WS consumer on ChannelDtmfReceived). Collects up to `max_digits`
        with `timeout_s` as BOTH the first-digit and the inter-digit timeout. Digits pressed
        during the prompt (barge-in / type-ahead) are already queued and count. Returns the
        digit string, or None on no input (-> the menu's 'timeout' port)."""
        if prompt:
            await self.play(channel_id, str(prompt))
        queue = dtmf.digit_queue(channel_id)
        if queue is None:
            # No consumer registered this channel (client driven outside a flow run):
            # behave like a timeout so the menu falls through, never dead air.
            return None
        max_digits = max(1, int(max_digits or 1))
        timeout_s = max(0.5, float(timeout_s or 5))
        digits = ""
        while len(digits) < max_digits:
            try:
                digit = await asyncio.wait_for(queue.get(), timeout=timeout_s)
            except asyncio.TimeoutError:
                break
            if digit:
                digits += str(digit)
        return digits or None

    async def dial_number(self, channel_id: str, number: str, *, caller_id, timeout_s: float) -> str:
        """Real Forward-to-Phone (Ticket 15.3): originate + bridge, observed over the WS.

        The outbound leg is originated on the BulkVS PJSIP trunk INTO OUR OWN Stasis app,
        marked as a flow-dial leg (appArgs + channel-id prefix) so the consumer never treats
        it as a fresh inbound call, and linked onto the inbound call via `originator` — which
        (a) collapses it onto the same Linkedid/calls row and (b) makes ARI reuse the inbound
        caller's callerid on the outbound leg (caller-ID PASSTHROUGH) unless the node config
        supplies an explicit `caller_id` override.

        Outcome -> port: answered (bridged; returns after either leg leaves), "busy"
        (Q.850 17), "noanswer" (ring timeout / no-answer causes), "failed" (originate
        rejection or any error). Best-effort throughout — never raises into the flow."""
        out_id = f"{FLOW_DIAL_CHANNEL_PREFIX}{uuid.uuid4().hex}"
        timeout_s = max(1.0, float(timeout_s or 25))
        queue = dtmf.watch(out_id, channel_id)
        bridge_id: str | None = None
        try:
            params = {
                "endpoint": f"PJSIP/{number}@{settings.BULKVS_TRUNK_NAME}",
                "app": settings.ARI_APP,
                "appArgs": FLOW_DIAL_APP_ARG,
                "channelId": out_id,
                "originator": channel_id,
                "timeout": str(int(timeout_s)),
            }
            if caller_id:
                params["callerId"] = str(caller_id)
            created = await self._post_json("/ari/channels", params=params)
            if not isinstance(created, dict) or not created.get("id"):
                return "failed"

            port = await self._await_dial_answer(queue, channel_id, out_id, timeout_s)
            if port != "answered":
                return port

            bridge_id = await self.create_bridge()
            if not bridge_id:
                return "failed"
            await self.add_to_bridge(bridge_id, channel_id, out_id)
            await self._await_bridge_end(queue, channel_id, out_id)
            return "answered"
        except Exception:  # noqa: BLE001 - a dial failure must fall through, never crash the call
            logger.exception("ARI dial_number to %s failed", number)
            return "failed"
        finally:
            # Never leak: detach the watcher, tear down the bridge, and drop the outbound
            # leg (DELETE on an already-gone channel is a harmless best-effort 404).
            dtmf.unwatch(queue, out_id, channel_id)
            if bridge_id:
                await self.destroy_bridge(bridge_id)
            await self._delete(f"/ari/channels/{out_id}")

    async def _await_dial_answer(
        self, queue: asyncio.Queue, channel_id: str, out_id: str, timeout_s: float
    ) -> str:
        """Watch the dialed leg's events until it answers or dies. Returns a dial port.
        Answer = the leg's StasisStart (an originate-with-app channel enters Stasis on
        answer) or a ChannelStateChange to Up, whichever the WS delivers first."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s + _DIAL_ANSWER_GRACE_S
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return "noanswer"
            try:
                event = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return "noanswer"
            etype = event.get("type")
            ch = event.get("channel") if isinstance(event.get("channel"), dict) else {}
            cid = str(ch.get("id") or "")
            if cid == channel_id:
                if etype in _LEG_GONE_EVENTS:
                    return "failed"  # the caller hung up while we were ringing the target
                continue
            if cid != out_id:
                continue
            if etype == "ChannelDestroyed":
                try:
                    cause = int(event.get("cause"))
                except (TypeError, ValueError):
                    cause = None
                return _DIAL_CAUSE_PORTS.get(cause, "failed")
            if etype == "StasisStart":
                return "answered"
            if etype == "ChannelStateChange" and str(ch.get("state") or "").lower() == "up":
                return "answered"

    async def _await_bridge_end(
        self, queue: asyncio.Queue, channel_id: str, out_id: str
    ) -> None:
        """Block while the two legs talk; return as soon as EITHER leg leaves the call."""
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_DIAL_BRIDGE_MAX_S)
            except asyncio.TimeoutError:
                logger.warning("ARI dial bridge exceeded %.0fs; tearing down", _DIAL_BRIDGE_MAX_S)
                return
            ch = event.get("channel") if isinstance(event.get("channel"), dict) else {}
            cid = str(ch.get("id") or "")
            if cid in (channel_id, out_id) and event.get("type") in _LEG_GONE_EVENTS:
                return

    async def dial_operator(
        self, channel_id: str, operators: list[str], *, caller_id, timeout_s: float
    ) -> str:
        """Dial operator browser legs (the `dial` operator-target node) and BRIDGE the caller to
        the first that answers (Ticket 18 — replaces the old originate-only stub). Rings every
        `PJSIP/operator-<slug>` at once; the first to answer is bridged to the caller and the
        rest are hung up; an offline/unregistered (or app-toggled-off) endpoint never answers.

        Outcome -> port: "answered" (bridged; returns after either leg leaves), "noanswer"
        (nobody answered / all unavailable / caller hung up while ringing), "failed" (error).
        Caller-ID passthrough: with no explicit `caller_id`, the `originator` link makes ARI
        reuse the inbound caller's callerid on each operator leg, so operators see who's calling."""
        endpoints = [operator_dial_endpoint(op) for op in operators if op]
        if not endpoints:
            return "failed"
        return await self.ring_and_bridge(
            channel_id, endpoints, caller_id=caller_id, timeout_s=timeout_s
        )

    async def available_operators(self) -> list[str]:
        """The `PJSIP/operator-<slug>` endpoints whose browser softphone is CURRENTLY registered
        (Ticket 18 presence). Availability = SIP registration: the InCallBar toggle registers/
        unregisters the endpoint, so an ARI /endpoints state of 'online' means a live contact is
        ready to ring. Best-effort: any error -> [] (the default handler then goes to voicemail)."""
        url = f"{self._base}/ari/endpoints"
        try:
            async with await self._client() as client:
                resp = await client.get(url)
                if resp.status_code >= 300:
                    return []
                data = resp.json()
        except Exception:  # noqa: BLE001 - presence probe is best-effort
            logger.exception("ARI available_operators query failed")
            return []
        out: list[str] = []
        for ep in data if isinstance(data, list) else []:
            if not isinstance(ep, dict):
                continue
            resource = str(ep.get("resource") or "")
            tech = str(ep.get("technology") or "").upper()
            state = str(ep.get("state") or "").lower()
            if tech == "PJSIP" and resource.startswith("operator-") and state == "online":
                out.append(f"PJSIP/{resource}")
        return out

    async def ring_and_bridge(
        self, channel_id: str, endpoints: list[str], *, caller_id, timeout_s: float,
        record_name: str | None = None,
    ) -> str:
        """Ring EVERY endpoint at once; bridge the caller to the first that answers, hang up the
        rest, optionally record the bridge, then block until either bridged leg leaves (Ticket 18).

        Each operator leg is originated INTO OUR OWN Stasis app, marked as a flow-dial leg
        (appArgs + channel-id prefix) so the consumer never treats it as a fresh inbound call,
        and linked onto the caller via `originator` (collapses onto the caller's Linkedid + gives
        caller-ID passthrough). Returns "answered" | "noanswer" | "failed"."""
        endpoints = [e for e in endpoints if e]
        if not endpoints:
            return "noanswer"
        timeout_s = max(1.0, float(timeout_s or 25))
        legs = {f"{FLOW_DIAL_CHANNEL_PREFIX}{uuid.uuid4().hex}": ep for ep in endpoints}
        watch_ids = list(legs.keys()) + [channel_id]
        queue = dtmf.watch(*watch_ids)
        bridge_id: str | None = None
        try:
            for out_id, endpoint in legs.items():
                params = {
                    "endpoint": endpoint,
                    "app": settings.ARI_APP,
                    "appArgs": FLOW_DIAL_APP_ARG,
                    "channelId": out_id,
                    "originator": channel_id,
                    "timeout": str(int(timeout_s)),
                }
                if caller_id:
                    params["callerId"] = str(caller_id)
                await self._post_json("/ari/channels", params=params)

            answered_id = await self._await_first_answer(
                queue, channel_id, set(legs.keys()), timeout_s
            )
            if answered_id is None:
                return "noanswer"

            # Winner found: hang up every other still-ringing operator leg.
            for out_id in legs:
                if out_id != answered_id:
                    await self._delete(f"/ari/channels/{out_id}")

            bridge_id = await self.create_bridge()
            if not bridge_id:
                return "failed"
            await self.add_to_bridge(bridge_id, channel_id, answered_id)
            if record_name:
                await self.record_bridge(bridge_id, record_name)
            await self._await_bridge_end(queue, channel_id, answered_id)
            return "answered"
        except Exception:  # noqa: BLE001 - a ring/bridge failure falls through, never crashes the call
            logger.exception("ARI ring_and_bridge failed")
            return "failed"
        finally:
            dtmf.unwatch(queue, *watch_ids)
            if bridge_id:
                await self.destroy_bridge(bridge_id)
            for out_id in legs:  # drop any leg still up (winner after bridge-end, or leftovers)
                await self._delete(f"/ari/channels/{out_id}")

    async def _await_first_answer(
        self, queue: asyncio.Queue, channel_id: str, out_ids: set[str], timeout_s: float
    ) -> str | None:
        """Watch all ringing operator legs; return the channel id of the FIRST to answer, or
        None if the caller hangs up, all legs die, or the ring times out. Answer = the leg's
        StasisStart (originate-with-app enters Stasis on answer) or a ChannelStateChange to Up."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s + _DIAL_ANSWER_GRACE_S
        pending = set(out_ids)
        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                event = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            etype = event.get("type")
            ch = event.get("channel") if isinstance(event.get("channel"), dict) else {}
            cid = str(ch.get("id") or "")
            if cid == channel_id:
                if etype in _LEG_GONE_EVENTS:
                    return None  # caller hung up while we were ringing operators
                continue
            if cid not in pending:
                continue
            if etype == "ChannelDestroyed":
                pending.discard(cid)  # this operator declined / no-answer / busy
                continue
            if etype == "StasisStart":
                return cid
            if etype == "ChannelStateChange" and str(ch.get("state") or "").lower() == "up":
                return cid
        return None

    async def ring_start(self, channel_id: str) -> None:
        """Send ringing indication to a channel (caller hears ringback while operators ring)."""
        await self._post(f"/ari/channels/{channel_id}/ring")

    async def ring_stop(self, channel_id: str) -> None:
        await self._delete(f"/ari/channels/{channel_id}/ring")

    async def play_and_wait(self, channel_id: str, media: str, *, timeout_s: float = 30.0) -> None:
        """Play a prompt and BLOCK until it finishes (PlaybackFinished over the WS) — so a
        consent notice completes before operators ring and a voicemail greeting completes before
        the record beep. Best-effort: an unplayable prompt or a missing finished-event (timeout)
        just returns, never dead-airs."""
        uri = await self._resolve_media(str(media))
        if not uri:
            return
        data = await self._post_json(
            f"/ari/channels/{channel_id}/play", params={"media": uri}
        )
        pb_id = data.get("id") if isinstance(data, dict) else None
        if not pb_id:
            return
        wait_queue = dtmf.register_playback(str(pb_id))
        try:
            await asyncio.wait_for(wait_queue.get(), timeout=timeout_s)
        except asyncio.TimeoutError:
            pass
        finally:
            dtmf.unregister_playback(str(pb_id))

    async def voicemail(
        self, channel_id: str, *, greeting, name: str,
        max_duration_s: float, max_silence_s: float,
    ) -> None:
        """Take a voicemail (Ticket 18 — the real capture the old node stub lacked): play the
        greeting to completion, then record the caller with a beep until they hang up or fall
        silent (capped at max_duration), then hang up. The WAV is named `{linkedid}-...` by the
        caller so RecordingFinished rides the existing recording->transcribe->analyze pipeline.

        Best-effort throughout: a greeting/record failure still hangs up cleanly (never dead air)."""
        try:
            if greeting:
                await self.play_and_wait(channel_id, str(greeting))
            rec_queue = dtmf.register_recording(str(name))
            watch_queue = dtmf.watch(channel_id)
            try:
                started = await self._post(
                    f"/ari/channels/{channel_id}/record",
                    params={
                        "name": name,
                        "format": "wav",
                        "ifExists": "overwrite",
                        "beep": "true",
                        "maxSilenceSeconds": str(int(max_silence_s)),
                        "maxDurationSeconds": str(int(max_duration_s)),
                    },
                )
                if not started:
                    return
                await self._await_voicemail_end(
                    rec_queue, watch_queue, channel_id, float(max_duration_s)
                )
            finally:
                dtmf.unregister_recording(str(name))
                dtmf.unwatch(watch_queue, channel_id)
        except Exception:  # noqa: BLE001 - voicemail is best-effort; always hang up cleanly
            logger.exception("ARI voicemail failed for %s", channel_id)
        finally:
            await self.hangup(channel_id)

    async def _await_voicemail_end(
        self, rec_queue: asyncio.Queue, watch_queue: asyncio.Queue,
        channel_id: str, max_duration_s: float,
    ) -> None:
        """Block until the voicemail recording finishes (silence/maxDuration -> RecordingFinished)
        OR the caller hangs up, whichever comes first (bounded by max_duration + grace)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_duration_s + 5.0
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            getters = {
                asyncio.ensure_future(rec_queue.get()),
                asyncio.ensure_future(watch_queue.get()),
            }
            done, pendingfs = await asyncio.wait(
                getters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            for fut in pendingfs:
                fut.cancel()
            if not done:
                return  # timed out
            event = next(iter(done)).result()
            etype = event.get("type") if isinstance(event, dict) else None
            if etype == "RecordingFinished":
                return  # caller went silent / hit the cap; recording saved
            if etype in _LEG_GONE_EVENTS:
                return  # caller hung up; Asterisk finalizes the recording

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

"""owen-voice entrypoint — control API + ARI event consumer + AudioSocket server.

Step 1 of AI_AGENT_SPEC: prove that audio leaves Asterisk, crosses a process boundary, and
returns in time to sound like a phone call. Nothing here talks to OWEN, to a database, or to
any AI vendor — if this works, the rest of the spec is scheduling.

Three things run in one asyncio process:
  1. the AudioSocket TCP server (app/server.py)
  2. an ARI events WebSocket consumer on our OWN Stasis app (never OWEN's)
  3. a small HTTP control API to start a self-test call and read the evidence
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.ari import AriClient
from app.config import settings
from app.server import start_server
from app.session import MediaSession, registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("voice")

ari = AriClient()

# Correlation maps for the ARI consumer. A spike call has TWO channels entering our Stasis
# app — the call leg and the external-media leg — and they need different handling.
_call_legs: dict[str, MediaSession] = {}
_media_legs: dict[str, MediaSession] = {}

_BACKOFF_MIN, _BACKOFF_MAX = 1.0, 30.0


# --- ARI event handling --------------------------------------------------------------------

async def _on_call_leg_start(channel_id: str, session: MediaSession) -> None:
    """The call leg answered and entered Stasis. Answer it, then attach media."""
    session.call_channel_id = channel_id
    await ari.answer(channel_id)

    media_id = await ari.create_external_media(session_uuid=session.session_uuid)
    if not media_id:
        session.error = "externalMedia creation failed"
        logger.error("spike %s: externalMedia failed; hanging up", session.session_uuid)
        await ari.hangup(channel_id)
        return
    session.media_channel_id = media_id
    _media_legs[media_id] = session
    logger.info("spike %s: externalMedia channel %s created", session.session_uuid, media_id)
    # Bridging is deliberately NOT done here — see _on_media_leg_start.


async def _on_media_leg_start(channel_id: str, session: MediaSession) -> None:
    """The external-media leg entered Stasis. NOW it is safe to bridge.

    Bridging before a leg is in Stasis is precisely the race the backend already paid for on
    outbound calls: ARI returns from originate before the channel arrives, addChannel then
    fails with 422/409, and the legs never join. `handle_outbound_call` documents it. Same
    rule applies here, so we wait for the event rather than assuming.
    """
    if not session.call_channel_id:
        return
    bridge_id = await ari.create_bridge()
    if not bridge_id:
        session.error = "bridge creation failed"
        logger.error("spike %s: bridge failed; hanging up", session.session_uuid)
        await ari.hangup(session.call_channel_id)
        return
    session.bridge_id = bridge_id
    ok = await ari.add_to_bridge(bridge_id, session.call_channel_id, channel_id)
    if not ok:
        session.error = "addChannel failed"
        logger.error("spike %s: addChannel failed; tearing down", session.session_uuid)
        await _teardown(session)
        return
    logger.info("spike %s: bridged call=%s media=%s in %s — speak now",
                session.session_uuid, session.call_channel_id, channel_id, bridge_id)


async def _teardown(session: MediaSession) -> None:
    """Best-effort cleanup. Always safe to call twice."""
    if session.media_channel_id:
        await ari.hangup(session.media_channel_id)
    if session.call_channel_id:
        await ari.hangup(session.call_channel_id)
    if session.bridge_id:
        await ari.destroy_bridge(session.bridge_id)
    _call_legs.pop(session.call_channel_id or "", None)
    _media_legs.pop(session.media_channel_id or "", None)


async def _handle_event(event: dict) -> None:
    etype = event.get("type")
    ch = event.get("channel") if isinstance(event.get("channel"), dict) else {}
    channel_id = str(ch.get("id") or "")
    if not channel_id:
        return

    if etype == "StasisStart":
        if channel_id in _call_legs:
            await _on_call_leg_start(channel_id, _call_legs[channel_id])
        elif channel_id in _media_legs:
            await _on_media_leg_start(channel_id, _media_legs[channel_id])
        else:
            # Not ours. With our own Stasis app this should not happen — log and leave it
            # alone rather than hanging up a channel we do not understand.
            logger.warning("voice: StasisStart for unknown channel %s", channel_id)
        return

    if etype in ("StasisEnd", "ChannelDestroyed", "ChannelHangupRequest"):
        session = _call_legs.get(channel_id)
        if session is not None:
            logger.info("spike %s: call leg %s ended (%s) — %s",
                        session.session_uuid, channel_id, etype, session.verdict())
            await _teardown(session)


async def _run_consumer() -> None:
    """Stream ARI events forever, reconnecting with backoff. Mirrors the backend consumer's
    contract: one bad event never kills the loop, and a flapping Asterisk cannot hot-spin us."""
    import websockets

    backoff = _BACKOFF_MIN
    logger.info("voice: ARI consumer starting, app=%s", settings.ARI_APP)
    while True:
        try:
            async with websockets.connect(settings.ari_ws_url) as ws:
                logger.info("voice: connected to ARI events WS")
                backoff = _BACKOFF_MIN
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                        if isinstance(event, dict):
                            await _handle_event(event)
                    except Exception:  # noqa: BLE001
                        logger.exception("voice: failed to handle ARI event")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("voice: ARI WS error (%s); reconnecting in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)


async def _guard_max_duration(session: MediaSession) -> None:
    """Hard ceiling on a spike call so a forgotten test cannot hold a trunk channel. Your
    trunk allows 10 concurrent inbound (BulkVS /trunkGroups MaxIn) — a stuck test is a real
    cost, not a theoretical one."""
    await asyncio.sleep(settings.MAX_CALL_SECONDS)
    if session.closed_at is None and session.call_channel_id:
        logger.warning("spike %s: max duration %ds reached; hanging up",
                       session.session_uuid, settings.MAX_CALL_SECONDS)
        await _teardown(session)


# --- HTTP control API ------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    server = await start_server()
    consumer = asyncio.create_task(_run_consumer())
    try:
        yield
    finally:
        consumer.cancel()
        server.close()
        await server.wait_closed()


app = FastAPI(title="owen-voice (echo spike)", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "ari_reachable": await ari.ping(),
        "stasis_app": settings.ARI_APP,
        "audiosocket_listen": f"{settings.AUDIOSOCKET_BIND}:{settings.AUDIOSOCKET_PORT}",
        "audiosocket_advertise": settings.AUDIOSOCKET_ADVERTISE,
        "media_format": settings.MEDIA_FORMAT,
        "active_sessions": len(registry.active()),
    }


@app.get("/sessions")
async def sessions() -> dict:
    """Every session this process has seen, newest last. This is the evidence: `verdict`
    answers "did the transport work" without anyone needing to interpret raw counters."""
    return {"sessions": [s.snapshot() for s in registry.all()]}


class SpikeCallIn(BaseModel):
    to: str                      # E.164 number to ring — YOUR phone
    from_number: str | None = None   # owned DID for caller-ID; defaults to BULKVS_FROM_NUMBER


@app.post("/spike/call")
async def spike_call(body: SpikeCallIn) -> dict:
    """Place a self-test call and echo the caller's audio back to them.

    This is a call to YOUR OWN phone to prove a transport, not agent outbound calling — which
    AI_AGENT_SPEC scopes out entirely (see Scope / D8). Nothing here is a template for dialling
    anyone else.
    """
    if not settings.ARI_PASSWORD:
        raise HTTPException(503, "ARI_PASSWORD not configured")
    from_number = body.from_number or settings.FROM_NUMBER
    if not from_number:
        raise HTTPException(422, "from_number required (or set BULKVS_FROM_NUMBER)")
    to = body.to.strip()
    if not to:
        raise HTTPException(422, "to required")

    session = registry.create(label=f"spike->{to}")
    endpoint = f"PJSIP/{to}@{settings.TRUNK_NAME}"

    # Pre-assign the channel id so the StasisStart that follows is correlatable the moment it
    # arrives — the same pre-assignment trick handle_outbound_call uses, and for the same
    # reason: the event can land before the originate call returns.
    channel_id = session.session_uuid.replace("-", "")
    _call_legs[channel_id] = session
    session.call_channel_id = channel_id

    got = await ari.originate_to_stasis(
        endpoint, caller_id=from_number, channel_id=channel_id
    )
    if not got:
        _call_legs.pop(channel_id, None)
        session.error = "originate failed"
        raise HTTPException(502, "originate failed — check trunk name, DID and ARI logs")

    asyncio.create_task(_guard_max_duration(session))
    logger.info("spike %s: originated %s -> %s (channel %s)",
                session.session_uuid, from_number, to, channel_id)
    return {
        "ok": True,
        "session_uuid": session.session_uuid,
        "channel_id": channel_id,
        "next": f"answer the call, speak, then GET /sessions to read the verdict",
    }


@app.post("/spike/hangup/{session_uuid}")
async def spike_hangup(session_uuid: str) -> dict:
    session = registry.get(session_uuid)
    if session is None:
        raise HTTPException(404, "unknown session")
    await _teardown(session)
    return {"ok": True, "verdict": session.verdict()}

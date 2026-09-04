"""AudioSocket TCP server — the echo loop (Step 1).

Asterisk connects IN (ARI externalMedia `connection_type=client`), announces itself with a
UUID frame, then streams audio. For this spike we write every audio frame straight back, so a
caller hears themselves and the transport is proven end to end: Asterisk -> TCP -> this
process -> TCP -> Asterisk -> bridge -> caller.

The echo is the ONLY thing that changes when this becomes the real pipeline. Everything else
here — framing, correlation, teardown, counters — is what the cascaded engine will sit on.
"""

from __future__ import annotations

import asyncio
import logging

from app.audiosocket import (
    AUDIO_FRAME_BYTES,
    FrameParser,
    encode_audio,
)
from app.config import settings
from app.session import MediaSession, peak_of, registry
from app.pipeline import Conversation
from app.tone import sine_frame

logger = logging.getLogger("voice.audiosocket")

# A connection that never announces itself is unusable — we cannot know which call it is.
# Bounded so a stray port scan cannot hold a socket open indefinitely.
_UUID_TIMEOUT_S = 5.0

# Read chunk. Comfortably above one 320-byte frame so a busy stream is drained in few reads,
# while the FrameParser handles whatever split the kernel actually gives us.
_READ_SIZE = 4096


async def handle_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    peer = writer.get_extra_info("peername")
    parser = FrameParser()
    session: MediaSession | None = None
    pump: asyncio.Task | None = None
    convo: Conversation | None = None
    logger.info("audiosocket: connection from %s", peer)

    try:
        deadline = asyncio.get_running_loop().time() + _UUID_TIMEOUT_S
        while True:
            # Until the session is identified, cap the wait so an unannounced connection is
            # dropped rather than parked forever.
            timeout = None if session else max(0.1, deadline - asyncio.get_running_loop().time())
            try:
                data = await asyncio.wait_for(reader.read(_READ_SIZE), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("audiosocket: %s sent no UUID within %.0fs; closing",
                               peer, _UUID_TIMEOUT_S)
                return
            if not data:
                return  # peer closed

            for frame in parser.feed(data):
                if frame.is_uuid:
                    sid = frame.uuid_str()
                    session = registry.get(sid) if sid else None
                    if session is None:
                        # An unknown UUID means Asterisk connected for a session we never
                        # created (or one already reaped). Nothing safe to do but close.
                        logger.warning("audiosocket: unknown session uuid %s from %s", sid, peer)
                        return
                    session._writer = writer
                    session.connected_at = asyncio.get_running_loop().time()
                    logger.info("audiosocket: session %s connected (%s, mode=%s)",
                                sid, session.label, session.mode)
                    if session.mode == "tone":
                        pump = asyncio.create_task(_tone_pump(session, writer))
                    elif session.mode == "agent":
                        # Step 2: the cascaded pipeline replaces the echo. Greeting first,
                        # so the caller is never met with silence.
                        convo = Conversation(session, writer)
                        # A loopback self-test has no caller: greeting it would only make the
                        # agent talk over the very sound it is supposed to be listening to.
                        await convo.start(greet=session.call_channel_id is not None)
                    continue

                if session is None:
                    # Audio before identification: cannot attribute it, so ignore rather than
                    # guess. (Asterisk always leads with the UUID; this is belt-and-braces.)
                    continue

                if frame.is_audio:
                    session.rx_frames += 1
                    session.rx_bytes += len(frame.payload)
                    session.peak_amplitude = peak_of(frame.payload, session.peak_amplitude)

                    # Asterisk paces the stream at one frame per 20 ms, so responding per
                    # received frame keeps us naturally in step with real time — no timer,
                    # no drift. Step 2 replaces this block with STT -> LLM -> TTS; the
                    # frame-in/frame-out cadence stays exactly as it is.
                    if session.mode == "tone":
                        # The tone pump owns the send side on its own clock — see
                        # _tone_pump. Nothing to write here.
                        continue
                    if session.mode == "agent":
                        # Playout owns the send side; this only feeds turn detection.
                        ended = await convo.on_frame(frame.payload) if convo else None
                        if ended:
                            logger.info("audiosocket: session %s guardrail %s — ending",
                                        session.session_uuid, ended)
                            session.error = None
                            return
                        continue
                    writer.write(encode_audio(frame.payload))  # THE ECHO
                    session.tx_frames += 1
                    session.tx_bytes += len(frame.payload)
                    # Backpressure: without draining, a slow socket grows an unbounded buffer
                    # and latency climbs silently until the call sounds broken.
                    await writer.drain()
                    continue

                if frame.is_dtmf:
                    digit = frame.payload.decode("ascii", "ignore")
                    session.dtmf += digit
                    logger.info("audiosocket: session %s dtmf %r", session.session_uuid, digit)
                    continue

                if frame.is_terminate:
                    logger.info("audiosocket: session %s terminate frame", session.session_uuid)
                    return

                if frame.is_error:
                    session.error = f"asterisk error frame: {frame.payload!r}"
                    logger.warning("audiosocket: session %s error frame %r",
                                   session.session_uuid, frame.payload)
                    return

                if not frame.is_known:
                    logger.debug("audiosocket: ignoring unknown frame type 0x%02x", frame.kind)

    except (ConnectionResetError, BrokenPipeError):
        logger.info("audiosocket: %s reset", peer)
    except Exception:  # noqa: BLE001 - one bad connection must never kill the server
        logger.exception("audiosocket: connection handler failed (%s)", peer)
        if session is not None:
            session.error = "handler exception"
    finally:
        if pump is not None:
            pump.cancel()
        if convo is not None:
            await convo.close()
        if session is not None:
            session.closed_at = asyncio.get_running_loop().time()
            session._writer = None
            logger.info(
                "audiosocket: session %s closed — rx=%d frames/%dB tx=%d frames peak=%d — %s",
                session.session_uuid, session.rx_frames, session.rx_bytes,
                session.tx_frames, session.peak_amplitude, session.verdict(),
            )
            if parser.buffered:
                # A non-empty tail at close means we misread the stream somewhere.
                logger.warning("audiosocket: session %s left %d unparsed bytes",
                               session.session_uuid, parser.buffered)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - close is best-effort
            pass


async def _tone_pump(session: MediaSession, writer: asyncio.StreamWriter) -> None:
    """Emit a continuous sine on OUR OWN 20 ms clock, for the phone-free send-path proof.

    Deliberately not driven by received frames. A bridge whose only member is the
    external-media channel has no audio source, so it may deliver us nothing at all — and an
    rx-driven sender would then sit silent and look like a broken send path when in fact it
    was never asked to speak. Emitting on a timer is also what the real pipeline does: TTS
    speaks when it has something to say, not when the caller happens to send a packet.
    """
    phase = 0.0
    try:
        while session.closed_at is None:
            pcm, phase = sine_frame(phase)
            writer.write(encode_audio(pcm))
            session.tx_frames += 1
            session.tx_bytes += len(pcm)
            await writer.drain()
            await asyncio.sleep(0.02)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the pump must never take the connection down
        logger.exception("audiosocket: tone pump failed (session %s)", session.session_uuid)


async def start_server() -> asyncio.AbstractServer:
    server = await asyncio.start_server(
        handle_connection, settings.AUDIOSOCKET_BIND, settings.AUDIOSOCKET_PORT
    )
    logger.info(
        "audiosocket: listening on %s:%s (advertising %s to Asterisk, %d-byte frames)",
        settings.AUDIOSOCKET_BIND, settings.AUDIOSOCKET_PORT,
        settings.AUDIOSOCKET_ADVERTISE, AUDIO_FRAME_BYTES,
    )
    return server

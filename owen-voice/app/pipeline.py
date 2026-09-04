"""The cascaded conversation loop: STT -> LLM -> TTS, with barge-in (step 2).

This is what replaces the echo. Everything underneath it — framing, UUID correlation,
teardown, counters — is unchanged from step 1, which is the point of having proven the
transport first.

    caller audio ──▶ TurnDetector ──┬─ "start" ──▶ BARGE-IN: drop queued speech, abandon
                                    │                        the in-flight turn
                                    └─ "end"   ──▶ STT ──▶ LLM ──▶ TTS ──▶ Playout
                                                                              │
    caller  ◀───────────────── 20 ms frames on a drift-free clock ◀───────────┘

Two rules carried over from the spec, both about never leaving the caller in silence:
- Any stage failing is survivable. The turn is abandoned and the caller may speak again;
  nothing raises into the connection handler.
- A guardrail (max call / max silence) ends the call deliberately rather than hanging.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Optional

from app.audiosocket import AUDIO_FRAME_BYTES, encode_audio
from app.config import settings
from app.dsp import TurnDetector, chunk_frames, rms_of
from app.providers import get_llm, get_stt, get_tts
from app.session import MediaSession

logger = logging.getLogger("voice.pipeline")

FRAME_PERIOD_S = 0.02


class Playout:
    """Outbound audio queue drained at a drift-free 20 ms cadence.

    Deadline-based rather than `sleep(0.02)` per frame: sleeping a fixed interval *after*
    doing work makes every frame take slightly longer than 20 ms, so a 10-second reply drifts
    audibly behind real time and the far end's jitter buffer starts discarding. Advancing a
    deadline absorbs the work instead.

    `clear()` is barge-in: the caller started talking, so everything still queued is now
    something they do not want to hear.
    """

    def __init__(self, session: MediaSession, writer: asyncio.StreamWriter) -> None:
        self._q: deque[bytes] = deque()
        self._session = session
        self._writer = writer
        self._task: Optional[asyncio.Task] = None

    def enqueue(self, pcm: bytes) -> int:
        frames = chunk_frames(pcm, AUDIO_FRAME_BYTES)
        self._q.extend(frames)
        return len(frames)

    def clear(self) -> int:
        dropped = len(self._q)
        self._q.clear()
        return dropped

    @property
    def speaking(self) -> bool:
        return bool(self._q)

    def start(self) -> None:
        self._task = asyncio.create_task(self._pump())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _pump(self) -> None:
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        try:
            while True:
                next_at += FRAME_PERIOD_S
                if self._q:
                    frame = self._q.popleft()
                    self._writer.write(encode_audio(frame))
                    self._session.tx_frames += 1
                    self._session.tx_bytes += len(frame)
                    await self._writer.drain()
                await asyncio.sleep(max(0.0, next_at - loop.time()))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the pump must never take the connection down
            logger.exception("playout: pump failed (session %s)", self._session.session_uuid)


class Conversation:
    """One caller, one agent, for the life of a connection."""

    def __init__(self, session: MediaSession, writer: asyncio.StreamWriter) -> None:
        self.session = session
        self.playout = Playout(session, writer)
        self.vad = TurnDetector(
            speech_rms=settings.VAD_SPEECH_RMS,
            end_frames=settings.VAD_END_FRAMES,
        )
        self.stt = get_stt()
        self.llm = get_llm()
        self.tts = get_tts()
        self.history: list[dict] = []
        self._turn: Optional[asyncio.Task] = None
        self._started = time.monotonic()
        self._last_voice = time.monotonic()

    # --- lifecycle ---

    async def start(self, *, greet: bool = True) -> None:
        self.playout.start()
        greeting = settings.AGENT_GREETING.strip() if greet else ""
        if greeting:
            # Spoken before anything is heard, so the caller is never met with silence — and
            # it is where an AI disclosure belongs (spec D8; required in the EU since Aug 2026
            # and simply good manners everywhere else).
            self.history.append({"role": "assistant", "content": greeting})
            await self._speak(greeting)

    async def close(self) -> None:
        if self._turn is not None:
            self._turn.cancel()
        self.playout.stop()

    # --- inbound audio ---

    async def on_frame(self, pcm: bytes) -> Optional[str]:
        """Feed one 20 ms frame. Returns a reason string if a guardrail ended the call."""
        level = rms_of(pcm)
        self.session.rms_min = min(self.session.rms_min, level)
        self.session.rms_max = max(self.session.rms_max, level)
        self.session.rms_sum += level
        self.session.rms_n += 1

        event = self.vad.push(pcm)
        self.session.max_quiet_run = self.vad.max_quiet_run

        if event is not None and event[0] == "start":
            self._last_voice = time.monotonic()
            # BARGE-IN. The caller talking over the agent means the agent should stop, both
            # because it is rude not to and because everything queued was answering a
            # question they have moved on from.
            self.session.vad_starts += 1
            dropped = self.playout.clear()
            if dropped:
                logger.info("session %s: barge-in, dropped %d queued frames",
                            self.session.session_uuid, dropped)
            if self._turn is not None and not self._turn.done():
                self._turn.cancel()

        elif event is not None and event[0] == "end":
            self._last_voice = time.monotonic()
            self.session.vad_ends += 1
            audio = event[1] or b""
            self._turn = asyncio.create_task(self._handle_turn(audio))

        return self._guardrail()

    def _guardrail(self) -> Optional[str]:
        now = time.monotonic()
        if settings.AGENT_MAX_CALL_SECONDS and \
                now - self._started >= settings.AGENT_MAX_CALL_SECONDS:
            return "max_call_seconds"
        if settings.AGENT_MAX_SILENCE_SECONDS and \
                now - self._last_voice >= settings.AGENT_MAX_SILENCE_SECONDS:
            return "max_silence_seconds"
        return None

    # --- one turn ---

    async def _handle_turn(self, audio: bytes) -> None:
        """STT -> LLM -> TTS for one caller utterance. Cancellable at any point: a barge-in
        mid-turn should abandon the answer, not queue it up behind the caller's new question."""
        t0 = time.monotonic()
        try:
            text = await self.stt.transcribe(audio)
            if not text:
                logger.info("session %s: empty transcript, ignoring turn",
                            self.session.session_uuid)
                return
            t_stt = time.monotonic()
            self.session.turns += 1
            self.session.transcript.append({"speaker": "caller", "text": text})
            logger.info("session %s: caller: %s", self.session.session_uuid, text)

            self.history.append({"role": "user", "content": text})
            reply = await self.llm.reply(settings.AGENT_SYSTEM_PROMPT, self._trimmed_history())
            if not reply:
                logger.warning("session %s: empty LLM reply", self.session.session_uuid)
                return
            t_llm = time.monotonic()
            self.history.append({"role": "assistant", "content": reply})
            self.session.transcript.append({"speaker": "agent", "text": reply})
            logger.info("session %s: agent: %s", self.session.session_uuid, reply)

            frames = await self._speak(reply)
            t_tts = time.monotonic()
            logger.info(
                "session %s: turn latency stt=%dms llm=%dms tts=%dms total=%dms (%d frames)",
                self.session.session_uuid,
                int((t_stt - t0) * 1000), int((t_llm - t_stt) * 1000),
                int((t_tts - t_llm) * 1000), int((t_tts - t0) * 1000), frames,
            )
            self.session.last_turn_ms = int((t_tts - t0) * 1000)
        except asyncio.CancelledError:
            logger.info("session %s: turn abandoned (barge-in)", self.session.session_uuid)
            raise
        except Exception:  # noqa: BLE001 - a failed turn leaves the caller able to try again
            logger.exception("session %s: turn failed", self.session.session_uuid)

    def _trimmed_history(self) -> list[dict]:
        """Keep the last N turns only.

        The spec's cost warning is about exactly this: retained context grows input tokens
        every turn, so an untrimmed 20-minute call can cost several times a short one for the
        same words. A window is the simplest honest answer."""
        limit = settings.LLM_HISTORY_TURNS * 2
        return self.history[-limit:] if limit > 0 else self.history

    async def _speak(self, text: str) -> int:
        pcm = await self.tts.synthesize(text, settings.TTS_VOICE)
        if not pcm:
            return 0
        return self.playout.enqueue(pcm)

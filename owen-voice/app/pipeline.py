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
from app.dsp import (TurnDetector, chunk_frames, looks_like_english, rms_of,
                     split_speakable)
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
            end_frames=session.vad_end_frames or settings.VAD_END_FRAMES,
        )
        self.stt = get_stt()
        self.llm = get_llm()
        self.tts = get_tts()
        self.history: list[dict] = []
        self._turn: Optional[asyncio.Task] = None
        self._started = time.monotonic()
        self._last_voice = time.monotonic()
        # When the current burst of speech started playing, for the barge-in guard window.
        self._speaking_since: Optional[float] = None

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

        # Raise the bar for what counts as speech while WE are talking, so the agent cannot
        # interrupt itself. A real interruption is loud and sustained; an echo of our own
        # output is not.
        speaking = self.playout.speaking
        if speaking and (self.session.half_duplex
                         if self.session.half_duplex is not None
                         else settings.HALF_DUPLEX):
            # Half duplex: drop the frame before the detector ever sees it, and keep the
            # detector's state clean so the caller's first words after we stop are not
            # glued onto our own echo.
            self.vad.reset()
            self.session.half_duplex_dropped += 1
            return self._guardrail()
        if speaking and self._speaking_since is None:
            self._speaking_since = time.monotonic()
        elif not speaking:
            self._speaking_since = None
        self.vad.speech_rms = settings.VAD_SPEECH_RMS * (
            settings.VAD_BARGE_SCALE if speaking else 1.0
        )

        event = self.vad.push(pcm)
        self.session.max_quiet_run = self.vad.max_quiet_run

        if event is not None and event[0] == "start":
            # Guard window: the opening moments of our own reply are precisely when it would
            # come back through a speakerphone, so an "interruption" then is not believed.
            if (
                self._speaking_since is not None
                and (time.monotonic() - self._speaking_since) * 1000 < settings.BARGE_GUARD_MS
            ):
                self.session.barge_suppressed += 1
                logger.info("session %s: ignoring interruption inside the guard window",
                            self.session.session_uuid)
                self.vad.reset()
                return self._guardrail()
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
            # Energy floor before spending an STT call: a quiet utterance is line noise, and
            # Whisper-family models hallucinate fluent text from it.
            level = rms_of(audio)
            if level < settings.VAD_MIN_UTTERANCE_RMS:
                self.session.noise_utterances += 1
                logger.info("session %s: utterance rms %.0f below floor, discarding as noise",
                            self.session.session_uuid, level)
                return self._guardrail()
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
            if not looks_like_english(text):
                self.session.noise_utterances += 1
                logger.info("session %s: discarding non-English transcript %r as a "
                            "hallucination", self.session.session_uuid, text[:40])
                return
            t_stt = time.monotonic()
            self.session.turns += 1
            self.session.transcript.append({"speaker": "caller", "text": text})
            logger.info("session %s: caller: %s", self.session.session_uuid, text)

            self.history.append({"role": "user", "content": text})

            # PIPELINED: stream the reply, and hand each finished sentence to TTS while the
            # model is still writing the next one. The measured cost of NOT doing this was
            # llm(570ms) + tts(1300ms) of dead air before the caller heard a syllable; here
            # the first sentence starts speaking while the rest is still being generated.
            reply, frames, t_first = "", 0, None
            sentences: asyncio.Queue = asyncio.Queue()

            async def produce() -> None:
                """Drain the LLM stream into speakable sentences.

                A separate task on purpose. Calling _speak() inline inside the LLM loop stops
                us consuming further deltas until that sentence has finished synthesizing, so
                sentence 2 is not even being generated while sentence 1 speaks — which
                serialises exactly what this change exists to overlap."""
                buf = ""
                try:
                    async for piece in self.llm.reply_stream(
                        settings.AGENT_SYSTEM_PROMPT, self._trimmed_history()
                    ):
                        buf += piece
                        ready, buf = split_speakable(buf)
                        for s in ready:
                            await sentences.put(s)
                    if buf.strip():
                        await sentences.put(buf.strip())
                finally:
                    await sentences.put(None)   # end sentinel, even on failure

            producer = asyncio.create_task(produce())
            try:
                while True:
                    sentence = await sentences.get()
                    if sentence is None:
                        break
                    reply = f"{reply} {sentence}".strip()
                    n = await self._speak(sentence)
                    frames += n
                    if t_first is None and n:
                        t_first = time.monotonic()
            finally:
                producer.cancel()

            if not reply:
                # Streaming produced nothing (provider without SSE, or an error). Fall back to
                # the blocking call rather than leaving the caller unanswered.
                logger.info("session %s: stream produced nothing, falling back",
                            self.session.session_uuid)
                reply = await self.llm.reply(
                    settings.AGENT_SYSTEM_PROMPT, self._trimmed_history()
                )
                if not reply:
                    logger.warning("session %s: empty LLM reply", self.session.session_uuid)
                    return
                frames = await self._speak(reply)
                t_first = time.monotonic()

            t_done = time.monotonic()
            self.history.append({"role": "assistant", "content": reply})
            self.session.transcript.append({"speaker": "agent", "text": reply})
            logger.info("session %s: agent: %s", self.session.session_uuid, reply)

            # `first_audio` is the number the CALLER experiences — how long they waited in
            # silence. `total` is only when the last sentence finished synthesizing, which
            # they never notice because playback of the first one is already under way.
            first_ms = int(((t_first or t_done) - t0) * 1000)
            logger.info(
                "session %s: turn latency stt=%dms first_audio=%dms total=%dms (%d frames)",
                self.session.session_uuid, int((t_stt - t0) * 1000),
                first_ms, int((t_done - t0) * 1000), frames,
            )
            self.session.last_turn_ms = int((t_done - t0) * 1000)
            self.session.last_first_audio_ms = first_ms
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
        """Synthesize and queue one sentence, ENQUEUEING AS AUDIO ARRIVES.

        Streaming here matters as much as streaming the LLM: the playout pump can start
        emitting the opening syllables while the rest of the sentence is still being
        synthesized, so time-to-first-audio stops depending on sentence length. Falls back
        to the blocking call if streaming yields nothing, so a provider without streaming
        support is slower rather than mute."""
        voice = self.session.tts_voice or settings.TTS_VOICE
        instructions = self.session.tts_instructions or ""
        model = self.session.tts_model or ""
        frames = 0
        stream = getattr(self.tts, "synthesize_stream", None)
        if stream is not None:
            async for pcm in stream(text, voice, instructions=instructions, model=model):
                if pcm:
                    frames += self.playout.enqueue(pcm)
        if frames:
            return frames
        pcm = await self.tts.synthesize(text, voice, instructions=instructions, model=model)
        return self.playout.enqueue(pcm) if pcm else 0

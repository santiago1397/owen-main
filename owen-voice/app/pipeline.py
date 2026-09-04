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
from app.tools import TOOLS, enabled_tools, openai_schema
from app.session import MediaSession

logger = logging.getLogger("voice.pipeline")

FRAME_PERIOD_S = 0.02

# Frames buffered before playback starts (x20ms). 400ms comfortably covers the gap between
# streamed TTS chunks; it is added to time-to-first-audio, so it is the direct trade between
# latency and not stuttering.
PRIME_FRAMES = 20


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
        # JITTER BUFFER. Streamed TTS arrives in irregular bursts while the pump drains at a
        # strict 20ms. Emitting the instant the first chunk lands means the queue runs dry
        # mid-word whenever the next chunk is slow, and a gap inside a word is exactly what
        # "robotic" sounds like. So: wait until PRIME frames are queued (or the utterance is
        # complete) before starting, and re-prime after any underrun.
        #
        # This is the regression that arrived WITH streaming: the original non-streaming path
        # enqueued a whole reply at once and could never underrun, which is why the very first
        # live call sounded fine and every one after it did not.
        self._playing = False
        self._complete = False

    def enqueue(self, pcm: bytes) -> int:
        frames = chunk_frames(pcm, AUDIO_FRAME_BYTES)
        self._q.extend(frames)
        return len(frames)

    def mark_complete(self) -> None:
        """The current utterance is fully synthesized: play whatever is queued even if it is
        shorter than the prime threshold, so a two-word reply is not held back."""
        self._complete = True

    def begin_utterance(self) -> None:
        self._complete = False

    def clear(self) -> int:
        dropped = len(self._q)
        self._q.clear()
        self._playing = False
        self._complete = False
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
                if not self._playing:
                    # Hold until there is enough buffered to ride out a slow chunk, unless
                    # the utterance is already complete (nothing more is coming).
                    if len(self._q) >= PRIME_FRAMES or (self._complete and self._q):
                        self._playing = True
                elif not self._q:
                    # Queue empty. Only an UNDERRUN if more audio was still expected — if the
                    # utterance is complete this is simply the end of the sentence.
                    # (The counter previously fired on both, so it reported one "underrun"
                    # per completed sentence and made a healthy call look broken.)
                    if not self._complete:
                        self._session.underruns += 1
                    self._playing = False
                if self._playing and self._q:
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
        self.llm = get_llm(
            base_url=str((session.agent or {}).get("llm_base_url") or ""),
            model=str((session.agent or {}).get("model") or ""),
        )
        self.tts = get_tts()
        # The PINNED agent-version config sent by OWEN (step 3). Empty for a standalone
        # spike, in which case the env defaults stand in. Reading it per session is what
        # makes an "army" possible: persona, voice and model differ per agent, and the
        # version that ran is already recorded against the call by OWEN.
        self.agent: dict = session.agent or {}
        # Only the tools this agent VERSION toggled on. The registry is closed, so a stale
        # toggle cannot smuggle in a capability that does not exist.
        self.tools = enabled_tools(self.agent.get("tools"))
        self._tool_schema = (
            openai_schema(self.tools, self.agent.get("transfer_targets"))
            if self.tools else None
        )
        self.history: list[dict] = []
        self._turn: Optional[asyncio.Task] = None
        self._started = time.monotonic()
        self._last_voice = time.monotonic()
        # When the current burst of speech started playing, for the barge-in guard window.
        self._speaking_since: Optional[float] = None
        # Streamed tool-call fragments for the turn in flight, keyed by index.
        self._tool_calls: dict = {}

    # --- lifecycle ---

    @property
    def system_prompt(self) -> str:
        parts = [str(self.agent.get("persona") or settings.AGENT_SYSTEM_PROMPT).strip()]
        knowledge = str(self.agent.get("knowledge") or "").strip()
        if knowledge:
            parts.append("Reference knowledge:" + chr(10) + knowledge)
        return (chr(10)*2).join(p for p in parts if p)

    async def start(self, *, greet: bool = True) -> None:
        self.playout.start()
        greeting = (
            str(self.agent.get("greeting") or settings.AGENT_GREETING).strip() if greet else ""
        )
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
                        self.system_prompt, self._trimmed_history(), self._tool_schema
                    ):
                        if isinstance(piece, tuple) and piece and piece[0] == "tool":
                            self._collect_tool_delta(piece[1])
                            continue
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
                reply = await self.llm.reply(self.system_prompt, self._trimmed_history())
                if not reply:
                    logger.warning("session %s: empty LLM reply", self.session.session_uuid)
                    return
                frames = await self._speak(reply)
                t_first = time.monotonic()

            exit_port = self._dispatch_tools()
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

            if exit_port:
                # The agent asked to transfer or end. Let it finish the sentence it is
                # speaking — cutting a caller off mid-goodbye to route them is worse than
                # waiting a beat — then report the port and end the session.
                await self._drain_playout()
                self.session.result_port = exit_port
                self.session.done.set()
        except asyncio.CancelledError:
            logger.info("session %s: turn abandoned (barge-in)", self.session.session_uuid)
            raise
        except Exception:  # noqa: BLE001 - a failed turn leaves the caller able to try again
            logger.exception("session %s: turn failed", self.session.session_uuid)

    # --- tools -------------------------------------------------------------------------

    def _collect_tool_delta(self, call: dict) -> None:
        """Accumulate a streamed tool call. Arguments arrive as JSON fragments across many
        deltas, so they are concatenated by index and only parsed once the turn ends."""
        try:
            idx = int(call.get("index") or 0)
        except (TypeError, ValueError):
            idx = 0
        slot = self._tool_calls.setdefault(idx, {"name": "", "args": ""})
        fn = call.get("function") or {}
        if fn.get("name"):
            slot["name"] = fn["name"]
        if fn.get("arguments"):
            slot["args"] += fn["arguments"]

    def _dispatch_tools(self) -> Optional[str]:
        """Act on the tools the model called this turn. Returns an EXIT PORT if one of them
        was a flow-exit tool, else None.

        The agent never bridges or hangs up: it returns a port and the flow interpreter drives
        the graph edge. That separation is what keeps an LLM from being able to move a call
        somewhere nobody wired."""
        import json

        exit_port = None
        for slot in self._tool_calls.values():
            name = slot.get("name") or ""
            if name not in self.tools:
                continue    # not toggled on, or not in the registry at all
            try:
                args = json.loads(slot.get("args") or "{}")
            except ValueError:
                args = {}
            spec = TOOLS.get(name, {})
            if spec.get("kind") == "flow_exit":
                exit_port = spec.get("exit_port")
                # The destination the agent picked from its allowlist rides back with the
                # port; OWEN resolves the NAME to a real target and performs the move, so a
                # number never crosses this boundary (D9).
                if name == "transfer" and isinstance(args, dict) and args.get("destination"):
                    self.session.result_data["destination"] = str(args["destination"])
                logger.info("session %s: agent tool %s -> port %s",
                            self.session.session_uuid, name, exit_port)
                continue
            if name == "capture_lead" and isinstance(args, dict):
                clean = {k: v for k, v in args.items() if v not in (None, "")}
                if clean:
                    # Merged, not replaced: an agent that captures a name early and an address
                    # later has learned two things about one caller, not two different callers.
                    existing = self.session.result_data.get("captured") or {}
                    self.session.result_data["captured"] = {**existing, **clean}
                    logger.info("session %s: captured %s",
                                self.session.session_uuid, sorted(clean))
        self._tool_calls = {}
        return exit_port

    def _trimmed_history(self) -> list[dict]:
        """Keep the last N turns only.

        The spec's cost warning is about exactly this: retained context grows input tokens
        every turn, so an untrimmed 20-minute call can cost several times a short one for the
        same words. A window is the simplest honest answer."""
        limit = settings.LLM_HISTORY_TURNS * 2
        return self.history[-limit:] if limit > 0 else self.history

    async def _drain_playout(self, *, max_wait_s: float = 15.0) -> None:
        """Wait for queued speech to finish playing, bounded."""
        waited = 0.0
        while self.playout.speaking and waited < max_wait_s:
            await asyncio.sleep(0.1)
            waited += 0.1

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
        self.playout.begin_utterance()
        frames = 0
        # Collect the WHOLE sentence before queueing any of it. Enqueueing chunks as they
        # arrive is what produced gaps inside words: the pump drains at a strict 20ms and a
        # slow chunk empties the queue mid-syllable. A jitter buffer only narrows that window
        # — 400ms of priming still underran twice in a two-turn call — whereas a complete
        # sentence cannot underrun at all.
        #
        # The pipelining that actually pays is at the SENTENCE level, not the chunk level:
        # sentence 2 is synthesized while sentence 1 is still playing, so this costs only the
        # synthesis time of the FIRST sentence and nothing thereafter.
        stream = getattr(self.tts, "synthesize_stream", None)
        if stream is not None:
            parts = []
            async for pcm in stream(text, voice, instructions=instructions, model=model):
                if pcm:
                    parts.append(pcm)
            if parts:
                frames = self.playout.enqueue(b"".join(parts))
        if frames:
            self.playout.mark_complete()
            return frames
        pcm = await self.tts.synthesize(text, voice, instructions=instructions, model=model)
        n = self.playout.enqueue(pcm) if pcm else 0
        self.playout.mark_complete()
        return n

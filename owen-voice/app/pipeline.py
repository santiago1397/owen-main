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
from app.providers import (get_llm, get_stt, get_streaming_stt, get_tts,
                           streaming_stt_available)
from app.context import render_blob
from app.custom_tools import find as find_custom
from app.custom_tools import normalise as normalise_custom
from app.custom_tools import openai_schema as custom_schema
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
        # Per-agent provider pins (M4), layered under any env LOCK by resolve_provider. Read
        # from the PINNED version, so which vendor ran a given call stays answerable later.
        _agent_cfg = session.agent or {}
        _stt_pin = str(_agent_cfg.get("stt_provider") or "")
        _tts_pin = str(_agent_cfg.get("tts_provider") or "")
        self.stt = get_stt(_stt_pin)
        # Streaming STT drives its OWN turn detection (M1), so when one is configured the
        # local `self.vad` above is never consulted. It is still constructed because it is
        # the fallback if the socket refuses to open -- see start().
        self.stream_stt = get_streaming_stt(_stt_pin) if streaming_stt_available(_stt_pin) else None
        self._stt_pump: Optional[asyncio.Task] = None
        # EAGER MODE (M2): the reply drafted while the caller may still be talking, and the
        # transcript it was drafted from. Deepgram guarantees the EndOfTurn transcript matches
        # the EagerEndOfTurn one exactly, so the draft is reusable without comparison.
        self._draft: Optional[asyncio.Task] = None
        self._draft_for: str = ""
        # The playback gate. A turn may GENERATE freely; it may not put a frame on the wire
        # until this is set. Set by default so the local-VAD path (where a turn only ever
        # starts after the turn has definitively ended) is completely unaffected.
        self._commit = asyncio.Event()
        self._commit.set()
        # Idle watchdog state. Frame-driven guardrails cannot notice the absence of frames.
        self._last_frame = time.monotonic()
        self._watchdog: Optional[asyncio.Task] = None
        self.llm = get_llm(
            base_url=str((session.agent or {}).get("llm_base_url") or ""),
            model=str((session.agent or {}).get("model") or ""),
        )
        self.tts = get_tts(_tts_pin)
        # The PINNED agent-version config sent by OWEN (step 3). Empty for a standalone
        # spike, in which case the env defaults stand in. Reading it per session is what
        # makes an "army" possible: persona, voice and model differ per agent, and the
        # version that ran is already recorded against the call by OWEN.
        self.agent: dict = session.agent or {}
        # Only the tools this agent VERSION toggled on. The registry is closed, so a stale
        # toggle cannot smuggle in a capability that does not exist.
        self.tools = enabled_tools(self.agent.get("tools"))
        # Platform tools (closed registry) + this agent's own declared HTTP tools (D6).
        self.custom = normalise_custom(self.agent.get("custom_tools"))
        schema = openai_schema(self.tools, self.agent.get("transfer_targets")) if self.tools else []
        schema += custom_schema(self.custom)
        self._tool_schema = schema or None
        self.history: list[dict] = []
        self._turn: Optional[asyncio.Task] = None
        self._started = time.monotonic()
        self._last_voice = time.monotonic()
        # When the current burst of speech started playing, for the barge-in guard window.
        self._speaking_since: Optional[float] = None
        # Streamed tool-call fragments for the turn in flight, keyed by index.
        self._tool_calls: dict = {}
        # Custom tools chosen this turn, run after dispatch so an exit port is decided first.
        self._pending_custom: list = []

    # --- lifecycle ---

    @property
    def system_prompt(self) -> str:
        # Context first: what the model needs to know about WHO it is talking to comes before
        # how it should behave, and it is re-sent every turn, which is why render_blob caps it.
        parts = []
        if self.session.context_blob:
            parts.append(self.session.context_blob)
        parts.append(str(self.agent.get("persona") or settings.AGENT_SYSTEM_PROMPT).strip())
        knowledge = str(self.agent.get("knowledge") or "").strip()
        if knowledge:
            parts.append("Reference knowledge:" + chr(10) + knowledge)
        return (chr(10)*2).join(p for p in parts if p)

    async def start(self, *, greet: bool = True) -> None:
        self.playout.start()
        self._watchdog = asyncio.create_task(self._watch_idle())
        if self.stream_stt is not None:
            # DEGRADE, do not fail: a socket that will not open before the call has begun is
            # not the mid-call vendor switch M6 forbids -- nothing has been spoken yet, so
            # falling back to the local VAD changes no voice and strands no caller. It is
            # logged at WARNING because the trap is a deployment where Flux never connects
            # and every call still "works", 600ms slower, with nobody the wiser.
            if not await self.stream_stt.start():
                logger.warning("session %s: streaming STT unavailable, falling back to the "
                               "local turn detector", self.session.session_uuid)
                self.stream_stt = None
                self.session.stt_degraded = True
            else:
                self._stt_pump = asyncio.create_task(self._pump_turn_events())
        await self._resolve_context()
        greeting = (
            str(self.agent.get("greeting") or settings.AGENT_GREETING).strip() if greet else ""
        )
        if greeting:
            # Spoken before anything is heard, so the caller is never met with silence — and
            # it is where an AI disclosure belongs (spec D8; required in the EU since Aug 2026
            # and simply good manners everywhere else).
            self.history.append({"role": "assistant", "content": greeting})
            await self._speak(greeting)

    async def _resolve_context(self) -> None:
        """Merge OWEN's local half with the provider's, under a hard ceiling (C5).

        The lookup was started at media attach, so by now it is usually already done and this
        costs nothing. Past the ceiling we abandon it and greet generically -- a CRM must
        never dead-air a caller -- but we say so in the log, because the trap is an agent that
        quietly stops recognising anyone while every call still 'works' (C13)."""
        remote: dict = {}
        task = getattr(self.session, "_context_task", None)
        if task is not None:
            try:
                remote = await asyncio.wait_for(task, timeout=settings.CONTEXT_TIMEOUT_S)
            except asyncio.TimeoutError:
                self.session.context_degraded = True
                logger.warning(
                    "session %s: caller-context lookup exceeded %.1fs; greeting without it",
                    self.session.session_uuid, settings.CONTEXT_TIMEOUT_S,
                )
                task.cancel()
            except Exception:  # noqa: BLE001
                self.session.context_degraded = True
                logger.warning("session %s: caller-context lookup failed",
                               self.session.session_uuid, exc_info=True)
        if not isinstance(remote, dict):
            remote = {}
        if self.session.context_provider.get("url") and not remote:
            self.session.context_degraded = True

        blob, fields = render_blob(
            self.session.context, remote, self.session.context_provider.get("allowlist"),
        )
        self.session.context_blob = blob
        self.session.context_fields = fields
        if fields:
            # FIELD NAMES only -- never the values (C4). "Why did the agent know that?" stays
            # answerable without copying PII into app_logs as well as into the transcript.
            logger.info("session %s: caller context injected (%s)",
                        self.session.session_uuid, ", ".join(fields))

    async def _watch_idle(self) -> None:
        """End the session when frames STOP, which no other guardrail can detect.

        The caller hanging up does not always close the AudioSocket connection: Asterisk can
        leave the externalMedia channel Up, so there is no EOF, the read loop never returns,
        and every guardrail -- max_call, max_silence -- is stranded because each is only
        evaluated when a frame arrives. The session then holds one of MAX_SESSIONS slots until
        the request timeout, which is minutes of capacity for a call that already ended.

        Setting `done` is what unwinds it: POST /sessions returns and its finally hangs up the
        media channel, which closes the connection. `end_call`, not `failed` -- the caller
        leaving is a normal ending, and the flow should route on rather than treat it as an
        error.
        """
        idle = float(settings.AGENT_IDLE_SECONDS or 0)
        if idle <= 0:
            return
        try:
            while True:
                await asyncio.sleep(1.0)
                gap = time.monotonic() - self._last_frame
                if gap >= idle:
                    logger.info(
                        "session %s: no audio for %.1fs — the far end is gone, ending",
                        self.session.session_uuid, gap,
                    )
                    self.session.result_port = self.session.result_port or "end_call"
                    self.session.done.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a watchdog must never take the call down
            logger.exception("session %s: idle watchdog failed", self.session.session_uuid)

    async def close(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        if self._stt_pump is not None:
            self._stt_pump.cancel()
        if self.stream_stt is not None:
            # Best effort: the RTP is already gone by the time we get here, so a socket that
            # refuses to close politely is not worth delaying teardown for.
            await self.stream_stt.close()
        # Nothing may stay parked on the gate while we are tearing down.
        self._commit.set()
        if self._turn is not None:
            self._turn.cancel()
        self.playout.stop()

    # --- inbound audio ---

    async def on_frame(self, pcm: bytes) -> Optional[str]:
        """Feed one 20 ms frame. Returns a reason string if a guardrail ended the call."""
        self._last_frame = time.monotonic()
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

        # STREAMING STT (M1): the vendor owns turn detection, so the local detector below is
        # never consulted and turns arrive asynchronously via _pump_turn_events. Note this sits
        # AFTER the half-duplex drop above on purpose: while the agent is speaking those frames
        # are our own echo, and feeding them to Flux would let the agent end its own turn.
        if self.stream_stt is not None:
            await self.stream_stt.feed(pcm)
            return self._guardrail()

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

    # --- vendor-driven turns (M1/M2) ---

    async def _pump_turn_events(self) -> None:
        """Turn the streaming STT's events into turns.

        This replaces `on_frame`'s local-VAD branch entirely. The shape is the state machine
        Deepgram documents for eager end-of-turn, and the one rule that matters is that a
        DRAFT may be generated but never SPOKEN: a `resumed` arriving after audio started is
        the agent talking over a caller who never actually stopped.
        """
        stt = self.stream_stt
        if stt is None:
            return
        try:
            while True:
                ev = await stt.events.get()

                if ev.kind == "error":
                    # The socket is gone, and with it any ability to hear the caller. Do not
                    # limp on: pin the port to `failed` so the flow routes to default_fallback
                    # (voicemail), which is a DESIGNED outcome (M6) rather than an improvised
                    # one. Pinning it here wins over server.py's `or "end_call"` at close --
                    # this is not a graceful ending and must not be reported as one.
                    logger.warning("session %s: streaming STT failed mid-call",
                                   self.session.session_uuid)
                    self.session.stt_failed = True
                    self.session.error = "stt stream failed"
                    self.session.result_port = "failed"
                    self._commit.set()      # never leave a turn parked on the gate
                    return

                if ev.kind == "start":
                    self.session.vad_starts += 1
                    self._last_voice = time.monotonic()
                    # BARGE-IN, on semantics rather than energy. Under half duplex we never
                    # fed our own audio, so this can only be the caller.
                    dropped = self.playout.clear()
                    if dropped:
                        logger.info("session %s: barge-in, dropped %d queued frames",
                                    self.session.session_uuid, dropped)
                    self._cancel_turn("caller interrupted")

                elif ev.kind == "eager_end":
                    # DRAFT. Start the whole turn, but hold its audio at the gate below.
                    if ev.transcript and self._draft is None:
                        self._draft_for = ev.transcript
                        self._commit.clear()
                        self._draft = self._begin_turn(ev.transcript, drafted=True)

                elif ev.kind == "resumed":
                    # The prediction was wrong -- the caller kept talking. Nothing was spoken,
                    # so this costs one abandoned LLM call and no awkwardness.
                    self.session.eager_retracted += 1
                    self._cancel_turn("turn resumed")
                    self._commit.set()

                elif ev.kind == "end":
                    self._last_voice = time.monotonic()
                    if not ev.transcript:
                        continue
                    # Deepgram guarantees the EndOfTurn transcript matches the EagerEndOfTurn
                    # one exactly, so a live draft for the same text is reusable as-is. The
                    # inequality branch is defensive: if that contract ever breaks we start
                    # over rather than speak a reply to something the caller did not say.
                    if self._draft is not None and not self._draft.done():
                        if ev.transcript == self._draft_for:
                            self.session.eager_hits += 1
                            self._commit.set()      # release the audio it already generated
                            continue
                        self._cancel_turn("draft transcript did not match")
                    self._commit.set()
                    self._begin_turn(ev.transcript)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the pump must never take the call down
            logger.exception("session %s: turn pump failed", self.session.session_uuid)

    def _begin_turn(self, text: str, drafted: bool = False) -> asyncio.Task:
        self._turn = asyncio.create_task(self._handle_turn(b"", text=text, drafted=drafted))
        return self._turn

    def _cancel_turn(self, why: str) -> None:
        if self._turn is not None and not self._turn.done():
            logger.info("session %s: abandoning turn (%s)", self.session.session_uuid, why)
            self._turn.cancel()
        self._draft = None
        self._draft_for = ""

    def _guardrail(self) -> Optional[str]:
        # A dead STT stream ends the call on the NEXT frame rather than waiting for the
        # silence guardrail to notice 30s later. The port was already pinned to `failed` by
        # the pump, so the connection handler's `or "end_call"` cannot soften it.
        if self.session.stt_failed:
            return "stt_stream_failed"
        now = time.monotonic()
        if settings.AGENT_MAX_CALL_SECONDS and \
                now - self._started >= settings.AGENT_MAX_CALL_SECONDS:
            return "max_call_seconds"
        if settings.AGENT_MAX_SILENCE_SECONDS and \
                now - self._last_voice >= settings.AGENT_MAX_SILENCE_SECONDS:
            return "max_silence_seconds"
        return None

    # --- one turn ---

    async def _handle_turn(self, audio: bytes, text: str = "", drafted: bool = False) -> None:
        """STT -> LLM -> TTS for one caller utterance. Cancellable at any point: a barge-in
        mid-turn should abandon the answer, not queue it up behind the caller's new question.

        `text` short-circuits the STT leg: a streaming vendor has already produced the final
        transcript by the time it tells us the turn ended (M1), so there is nothing left to
        transcribe. That saved round trip -- 452-583ms measured on this host -- is the single
        largest term in the latency budget."""
        t0 = time.monotonic()
        try:
            u = self.session.usage
            if text:
                # Streaming: usage is the SOCKET's wall clock, not this utterance. Billing is
                # per connected minute, so summing utterances under-reports by ~2x (M9). It is
                # stamped once from the session rather than accumulated per turn.
                u["stt_model"] = settings.DG_STT_MODEL
                u["stt_audio_seconds"] = round(time.monotonic() - self._started, 2)
                u["stt_billing"] = "stream"
            else:
                # 8kHz, 16-bit mono: two bytes per sample.
                u["stt_model"] = settings.STT_MODEL
                u["stt_audio_seconds"] = round(
                    u.get("stt_audio_seconds", 0) + len(audio) / (8000 * 2), 2
                )
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
            # When the LLM produced its first SPEAKABLE sentence. Splits the wait the caller
            # experiences into the two halves we can actually act on -- the model thinking,
            # and the voice being synthesized -- which a single first_audio_ms cannot.
            t_sentence1 = None
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
                    if t_sentence1 is None:
                        t_sentence1 = time.monotonic()
                    reply = f"{reply} {sentence}".strip()
                    # THE PLAYBACK GATE (M2). Everything above this line -- the LLM stream,
                    # the sentence split -- may run on a merely PREDICTED turn end. Nothing
                    # below it may, because past here the caller hears us. On the local-VAD
                    # path this is already set and costs one no-op await.
                    await self._commit.wait()
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
            await self._run_custom_tools()
            t_done = time.monotonic()
            self.history.append({"role": "assistant", "content": reply})
            self.session.transcript.append({"speaker": "agent", "text": reply})
            logger.info("session %s: agent: %s", self.session.session_uuid, reply)

            # `first_audio` is the number the CALLER experiences — how long they waited in
            # silence. `total` is only when the last sentence finished synthesizing, which
            # they never notice because playback of the first one is already under way.
            first_ms = int(((t_first or t_done) - t0) * 1000)
            _t_s1 = t_sentence1 or t_done
            logger.info(
                "session %s: turn latency stt=%dms llm=%dms tts=%dms first_audio=%dms "
                "total=%dms (%d frames)",
                self.session.session_uuid, int((t_stt - t0) * 1000),
                int((_t_s1 - t_stt) * 1000), int(((t_first or t_done) - _t_s1) * 1000),
                first_ms, int((t_done - t0) * 1000), frames,
            )
            self.session.last_turn_ms = int((t_done - t0) * 1000)
            self.session.last_first_audio_ms = first_ms
            # The full breakdown, kept per turn. `stt_ms` is 0 on the streaming path by
            # construction (the transcript is final when EndOfTurn fires) -- that zero IS the
            # evidence Flux is doing its job, so it is recorded rather than omitted.
            # The decomposition. first_audio_ms is the number the caller feels; these are the
            # three terms it is made of, and without them "make it faster" has no target.
            # NOTE llm_ms and tts_ms OVERLAP with nothing: the stages are pipelined at the
            # SENTENCE level, so llm_ms is time-to-first-sentence (not the whole generation)
            # and tts_ms is that sentence's synthesis plus playout priming.
            t_s1 = t_sentence1 or t_done
            self.session.turn_metrics.append({
                "turn": len(self.session.turn_metrics) + 1,
                "stt_ms": int((t_stt - t0) * 1000),
                "llm_ms": int((t_s1 - t_stt) * 1000),
                "tts_ms": int(((t_first or t_done) - t_s1) * 1000),
                "first_audio_ms": first_ms,
                "total_ms": int((t_done - t0) * 1000),
                "frames": frames,
                "reply_chars": len(reply),
                "drafted": bool(drafted),
            })

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
            # Parsed BEFORE the registry check: a custom tool needs its arguments too, and
            # reading them only in the platform-tool branch left `args` unbound on the first
            # custom call — an UnboundLocalError swallowed by _handle_turn's except, so custom
            # tools silently never ran.
            try:
                args = json.loads(slot.get("args") or "{}")
            except ValueError:
                args = {}
            if name not in self.tools:
                if find_custom(self.custom, name):
                    self._pending_custom.append((name, args))
                continue    # not toggled on, or not in the registry at all
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

    async def _run_custom_tools(self) -> None:
        """Run the declared HTTP tools the model asked for this turn.

        A SYNC tool blocks the reply for at most ~800ms and speaks a filler first, so the
        silence is explained rather than dead. An ASYNC one is dispatched and forgotten: the
        caller gets an immediate acknowledgement and the work lands on its own, which is why
        writes default to that mode.

        The RESULT is appended to the conversation as context rather than spoken verbatim —
        the model decides what of it the caller actually needs to hear."""
        from app.providers import call_custom_tool

        pending, self._pending_custom = self._pending_custom, []
        for name, args in pending:
            tool = find_custom(self.custom, name)
            if tool is None:
                continue
            if tool["mode"] == "async":
                # Fire and forget. Deliberately not awaited: a write must never make a caller
                # wait, and its failure must never reroute a call.
                asyncio.create_task(call_custom_tool(tool, args))
                self.history.append({
                    "role": "system",
                    "content": f"[{name} was submitted and is being processed.]",
                })
                logger.info("session %s: async tool %s dispatched",
                            self.session.session_uuid, name)
                continue

            if tool.get("filler"):
                await self._speak(tool["filler"])
            status, body = await call_custom_tool(tool, args)
            self.session.tool_calls += 1
            snippet = str(body)[:800] if body is not None else "no response"
            self.history.append({
                "role": "system",
                "content": f"[{name} returned status {status}: {snippet}]",
            })
            logger.info("session %s: sync tool %s -> %s",
                        self.session.session_uuid, name, status)

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
        u = self.session.usage
        # Ask the ENGINE what it actually used, rather than assuming OpenAI's axes. For
        # Deepgram the voice IS the model (aura-2-thalia-en), so `model` is empty here and
        # recording settings.TTS_MODEL would attribute the spend to a vendor that never ran
        # -- which ai_cost.py would then price as unrated, or worse, price wrongly.
        resolve = getattr(self.tts, "resolved_model", None)
        u["tts_model"] = resolve(voice, model) if resolve else (model or settings.TTS_MODEL)
        u["tts_characters"] = u.get("tts_characters", 0) + len(text)
        frames = 0
        # Queue chunks AS THEY ARRIVE. `Playout` already refuses to start until PRIME_FRAMES
        # (400ms) is buffered and re-primes after any underrun, so holding the whole sentence
        # here as well was belt-and-braces on top of a guard that already exists -- and it was
        # expensive: measured on the first real call, first-audio sat 1685ms behind the TTS
        # response headers purely waiting for the last syllable of the sentence to synthesize.
        #
        # The earlier note (that 400ms of priming "still underran twice in a two-turn call")
        # was written against OpenAI TTS at 24kHz, which had to be resampled chunk by chunk in
        # this process. Aura-2 returns 8kHz natively at RTF ~0.111 -- roughly nine times faster
        # than playback -- so the queue refills far quicker than it drains. `session.underruns`
        # is the number that decides whether that holds; watch it on the next call, and if it
        # climbs, raise PRIME_FRAMES rather than going back to whole-sentence buffering.
        #
        # Sentence-level pipelining still applies on top: sentence 2 synthesizes while
        # sentence 1 plays.
        stream = getattr(self.tts, "synthesize_stream", None)
        if stream is not None:
            async for pcm in stream(text, voice, instructions=instructions, model=model):
                if pcm:
                    frames += self.playout.enqueue(pcm)
        if frames:
            self.playout.mark_complete()
            return frames
        pcm = await self.tts.synthesize(text, voice, instructions=instructions, model=model)
        n = self.playout.enqueue(pcm) if pcm else 0
        self.playout.mark_complete()
        return n

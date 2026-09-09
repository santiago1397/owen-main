"""STT / LLM / TTS behind three small seams (step 2, AI_AGENT_SPEC D11).

The cascaded pipeline's whole point is that each stage is independently swappable, so each is
a Protocol with a concrete implementation chosen by config. Nothing above this module knows
which vendor is in use.

Shipping against OpenAI + MiniMax because those keys already exist in .env.prod — no new
accounts. The spec's preferred stack (Deepgram Flux for STT, Aura-2/Cartesia for TTS) slots
in as additional classes here, changing nothing else. Flux additionally makes app/dsp.py's
TurnDetector deletable, since it does end-of-turn itself.

Every call is best-effort: a vendor failure returns empty/None and is logged. The caller
degrades (says nothing, or falls through) rather than dead-airing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Optional, Protocol

import httpx

from app.audiosocket import AUDIO_FRAME_BYTES
from app.config import settings
from app.dsp import Downsampler24to8, downsample_24k_to_8k, wav_unwrap, wav_wrap

logger = logging.getLogger("voice.providers")

# Generous vs the in-call budget on purpose: a slow answer is recoverable, a hung socket is
# not. The conversation layer is what enforces the felt latency.
_STT_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
_LLM_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
_TTS_TIMEOUT = httpx.Timeout(25.0, connect=5.0)


# --- STT -----------------------------------------------------------------------------------

class SpeechToText(Protocol):
    name: str

    async def transcribe(self, pcm8k: bytes) -> str: ...


class OpenAISTT:
    """OpenAI /audio/transcriptions over one utterance at a time.

    Utterance-at-a-time, not streaming: the local VAD already decides when a turn ended, so
    there is exactly one request per turn. It costs the round-trip that a streaming STT would
    have overlapped with the caller still speaking — which is precisely the 200-600ms the spec
    says Deepgram Flux buys back. Correct first, faster later.
    """

    name = "openai"

    async def transcribe(self, pcm8k: bytes) -> str:
        if not settings.OPENAI_API_KEY or not pcm8k:
            return ""
        files = {"file": ("turn.wav", wav_wrap(pcm8k), "audio/wav")}
        data = {"model": settings.STT_MODEL, "language": settings.STT_LANGUAGE}
        try:
            async with httpx.AsyncClient(timeout=_STT_TIMEOUT) as c:
                r = await c.post(
                    f"{settings.OPENAI_BASE_URL}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                    files=files, data=data,
                )
            if r.status_code >= 400:
                logger.warning("stt: %s %s", r.status_code, r.text[:200])
                return ""
            return (r.json().get("text") or "").strip()
        except Exception as exc:  # noqa: BLE001 - a failed turn must not kill the call
            logger.warning("stt: failed: %r", exc)
            return ""


# --- LLM -----------------------------------------------------------------------------------

class LanguageModel(Protocol):
    name: str

    async def reply(self, system: str, history: list[dict]) -> str: ...


# Which output-cap parameter a model wants. OpenAI's GPT-5+ families REJECT `max_tokens`
# outright -- "Unsupported parameter: 'max_tokens' is not supported with this model. Use
# 'max_completion_tokens' instead." -- with a 400, which in this pipeline means the reply
# stream yields nothing and the agent is SILENT for the whole call. The Model field in the
# agent editor is free text, so simply typing a current model name would have produced a mute
# agent with no clue why.
#
# Every OpenAI-COMPATIBLE third party (MiniMax, DeepSeek, Kimi, aggregators) still wants
# `max_tokens`, so this cannot be a blanket switch. We guess from the model name, then LEARN
# from a rejection: the 400 names the parameter, so one swap-and-retry fixes it permanently
# for that model and the cost is a single wasted request per process, once.
_MAX_TOKENS = "max_tokens"
_MAX_COMPLETION = "max_completion_tokens"
_NEW_PARAM_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")
_token_param_cache: dict[str, str] = {}


def _token_param_for(model: str) -> str:
    m = (model or "").strip().lower()
    if m in _token_param_cache:
        return _token_param_cache[m]
    return _MAX_COMPLETION if m.startswith(_NEW_PARAM_PREFIXES) else _MAX_TOKENS


def _wrong_token_param(status: int, body: str) -> bool:
    """Did the provider reject us specifically over the output-cap parameter?"""
    return status == 400 and "max_tokens" in body and "max_completion_tokens" in body


class OpenAICompatibleLLM:
    """Any OpenAI-compatible /chat/completions endpoint.

    This one class covers OpenAI, MiniMax, DeepSeek, Kimi/Moonshot and every aggregator
    (Together, Fireworks, Groq, OpenRouter) — they all speak the same wire format, which is
    exactly why D1 chose a cascaded pipeline: swapping the brain is a base_url and a model
    name. The backend already proves the pattern in analysis/classification.py.

    Remember the spec's latency note: a China-hosted endpoint costs ~200-250ms per turn from
    this European host. Prefer the same model on a Western host.
    """

    name = "openai_compatible"

    def __init__(self, base_url: str = "", model: str = "") -> None:
        # Per-agent overrides (step 3): an agent version may pin its own endpoint and model,
        # which is how one deployment runs OpenAI, MiniMax and DeepSeek agents side by side.
        self.base_url = (base_url or settings.LLM_BASE_URL).rstrip("/")
        self.model = model or settings.LLM_MODEL

    async def reply(self, system: str, history: list[dict]) -> str:
        if not settings.LLM_API_KEY:
            logger.warning("llm: no API key configured")
            return ""
        messages = [{"role": "system", "content": system}] if system else []
        messages += history
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": settings.LLM_TEMPERATURE,
            # Capped hard: this is speech. A model that decides to produce five paragraphs
            # makes the caller listen to all of it, and pays for TTS on every word.
            _token_param_for(self.model): settings.LLM_MAX_TOKENS,
        }
        try:
            async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as c:
                r = await c.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
                    json=payload,
                )
            if r.status_code >= 400:
                logger.warning("llm: %s %s", r.status_code, r.text[:200])
                return ""
            data = r.json()
            choices = data.get("choices") or []
            if not choices:
                return ""
            return (choices[0].get("message", {}).get("content") or "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm: failed: %r", exc)
            return ""

    async def reply_stream(self, system: str, history: list[dict], tools: list | None = None):
        """Yield reply text as the model produces it.

        This is half of the latency fix. Waiting for the whole reply before speaking a word
        means the caller hears nothing until the model has finished thinking; streaming lets
        the first sentence go to TTS while the rest is still being written. Falls back to the
        blocking path on any streaming failure, so a provider with flaky SSE degrades to
        slower rather than silent."""
        if not settings.LLM_API_KEY:
            return
        messages = [{"role": "system", "content": system}] if system else []
        messages += history
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": settings.LLM_TEMPERATURE,
            _token_param_for(self.model): settings.LLM_MAX_TOKENS,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        try:
            async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as c:
                async with c.stream(
                    "POST", f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
                    json=payload,
                ) as r:
                    if r.status_code >= 400:
                        body = (await r.aread())[:300]
                        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
                        # LEARN the right output-cap parameter rather than going mute. The
                        # provider's 400 names both spellings, so the correct one is knowable
                        # from the rejection itself -- and once cached, no later call pays for
                        # this. Without it, a model whose only sin is being current answers
                        # every turn with silence.
                        used = _token_param_for(self.model)
                        if _wrong_token_param(r.status_code, text):
                            other = _MAX_COMPLETION if used == _MAX_TOKENS else _MAX_TOKENS
                            _token_param_cache[(self.model or "").strip().lower()] = other
                            logger.warning("llm stream: %s wants %s, not %s — retrying and "
                                           "remembering", self.model, other, used)
                            async for piece in self.reply_stream(system, history, tools):
                                yield piece
                            return
                        logger.warning("llm stream: %s %s", r.status_code, text[:200])
                        return
                    async for line in r.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            continue
                        for ch in obj.get("choices") or []:
                            delta = ch.get("delta") or {}
                            piece = delta.get("content")
                            if piece:
                                yield piece
                            # Tool calls arrive as deltas too. Yielded as a tagged tuple so the
                            # conversation layer can act on them without this transport having
                            # to know what any tool MEANS.
                            for call in delta.get("tool_calls") or []:
                                yield ("tool", call)
        except Exception as exc:  # noqa: BLE001 - caller falls back to the blocking path
            logger.warning("llm stream: failed: %r", exc)
            return


# --- TTS -----------------------------------------------------------------------------------

class TextToSpeech(Protocol):
    name: str

    async def synthesize(self, text: str, voice: str,
                         instructions: str = "", model: str = "") -> bytes: ...


class OpenAITTS:
    """OpenAI /audio/speech -> 8 kHz PCM ready for AudioSocket.

    Asks for `wav` (24 kHz) and downsamples locally rather than shelling out to ffmpeg: this
    is the latency-critical path, and the backend's flow-TTS already shows how much operational
    weight an ffmpeg dependency carries. Returns raw 8 kHz PCM, or b"" on any failure —
    silence is recoverable, an exception here is not.
    """

    name = "openai"

    def resolved_model(self, voice: str = "", model: str = "") -> str:
        """What actually gets billed. Voice and model are separate axes here."""
        return model or settings.TTS_MODEL

    async def synthesize(self, text: str, voice: str,
                         instructions: str = "", model: str = "") -> bytes:
        text = (text or "").strip()
        if not text or not settings.OPENAI_API_KEY:
            return b""
        payload = {
            "model": model or settings.TTS_MODEL,
            "voice": voice or settings.TTS_VOICE,
            "input": text[: settings.TTS_MAX_CHARS],
            "response_format": "wav",
        }
        directive = instructions if instructions else settings.TTS_INSTRUCTIONS
        if directive:
            # Only the gpt-4o-mini-tts family honours this; older models ignore the field
            # rather than erroring, so it is safe to always send.
            payload["instructions"] = directive
        try:
            async with httpx.AsyncClient(timeout=_TTS_TIMEOUT) as c:
                r = await c.post(
                    f"{settings.OPENAI_BASE_URL}/audio/speech",
                    headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                    json=payload,
                )
            if r.status_code >= 400:
                logger.warning("tts: %s %s", r.status_code, r.text[:200])
                return b""
            return downsample_24k_to_8k(wav_unwrap(r.content))
        except Exception as exc:  # noqa: BLE001
            logger.warning("tts: failed: %r", exc)
            return b""

    async def synthesize_stream(self, text: str, voice: str,
                                instructions: str = "", model: str = ""):
        """Yield 8 kHz PCM as it is synthesized — the other half of the latency fix.

        Requests `pcm` rather than `wav`: the response is then raw 24 kHz little-endian
        samples with NO header, so the first bytes off the wire are already playable and
        there is nothing to wait for or parse. (A streamed `wav` would need its header
        first and the length field is only correct at the end.)

        Chunk boundaries are handled by dsp.Downsampler24to8, which carries the remainder —
        each chunk is an arbitrary byte count and the group-of-3 averaging needs whole
        groups, so dropping the remainder would tick at the chunk rate."""
        text = (text or "").strip()
        if not text or not settings.OPENAI_API_KEY:
            return
        payload = {
            "model": model or settings.TTS_MODEL,
            "voice": voice or settings.TTS_VOICE,
            "input": text[: settings.TTS_MAX_CHARS],
            "response_format": "pcm",
        }
        directive = instructions if instructions else settings.TTS_INSTRUCTIONS
        if directive:
            payload["instructions"] = directive
        down = Downsampler24to8()
        try:
            async with httpx.AsyncClient(timeout=_TTS_TIMEOUT) as c:
                async with c.stream(
                    "POST", f"{settings.OPENAI_BASE_URL}/audio/speech",
                    headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                    json=payload,
                ) as r:
                    if r.status_code >= 400:
                        body = (await r.aread())[:200]
                        logger.warning("tts stream: %s %s", r.status_code, body)
                        return
                    async for chunk in r.aiter_bytes():
                        out = down.feed(chunk)
                        if out:
                            yield out
            tail = down.flush()
            if tail:
                yield tail
        except Exception as exc:  # noqa: BLE001
            logger.warning("tts stream: failed: %r", exc)
            return


class DeepgramTTS:
    """Deepgram Aura-2 -> 8 kHz PCM, with NO resampling anywhere (M3).

    The whole reason this class exists is `sample_rate=8000&container=none`: Deepgram returns
    raw little-endian 16-bit samples already at telephony rate, so the bytes off the wire go
    straight into AudioSocket. OpenAITTS has to pull 24 kHz and run dsp.Downsampler24to8 over
    every chunk; this path has no downsampler at all, which removes both the CPU (on a box
    shared with Asterisk) and the bug class that produced the metallic-audio fault.

    REST rather than the Speak WebSocket on purpose: `synthesize_stream` is called once per
    SENTENCE, so a WS would be opened and torn down per sentence for no gain. Chunked REST
    gives the same first-byte behaviour with none of the lifecycle.

    Returns b"" / yields nothing on any failure -- silence is recoverable, an exception in a
    live call is not. Same contract as OpenAITTS.
    """

    name = "deepgram"

    def resolved_model(self, voice: str = "", model: str = "") -> str:
        """Deepgram has ONE axis: the voice IS the model (aura-2-thalia-en). An agent's stored
        `voice` therefore wins over `model`, which is the opposite of the OpenAI class.

        A voice belonging to ANOTHER provider falls back to the default instead of being sent.
        This is not defensive padding -- it happened on the first real agent: an agent carrying
        the OpenAI voice `alloy` was switched to Deepgram, we sent `model=alloy`, Deepgram
        answered 400 INVALID_QUERY_PARAMETER, synthesis returned b"" every turn, and the agent
        was SILENT on every reply with nothing in the call flow marking it as broken. Activation
        already warns that the default will be used; this is the half that makes that true.
        """
        chosen = (voice or model or "").strip()
        if chosen and not chosen.startswith("aura-"):
            logger.warning("dg tts: %r is not a Deepgram voice; using %s",
                           chosen, settings.DG_TTS_MODEL)
            return settings.DG_TTS_MODEL
        return chosen or settings.DG_TTS_MODEL

    def _params(self, model: str = "") -> dict:
        return {
            "model": model or settings.DG_TTS_MODEL,
            "encoding": "linear16",
            "sample_rate": "8000",
            # Without this Deepgram wraps linear16 in a WAV header, whose length field is only
            # correct once the whole utterance exists -- useless for streaming.
            "container": "none",
        }

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Token {settings.DEEPGRAM_API_KEY}"}

    async def synthesize(self, text: str, voice: str,
                         instructions: str = "", model: str = "") -> bytes:
        text = (text or "").strip()
        if not text or not settings.DEEPGRAM_API_KEY:
            return b""
        # `voice` IS the model for Deepgram (aura-2-thalia-en), unlike OpenAI where voice and
        # model are separate axes. An agent's stored voice wins; M5 resolves unknowns upstream.
        params = self._params(self.resolved_model(voice, model))
        try:
            async with httpx.AsyncClient(timeout=_TTS_TIMEOUT) as c:
                r = await c.post(settings.DG_TTS_REST_URL, params=params,
                                 headers=self._headers, json={"text": text[: settings.TTS_MAX_CHARS]})
            if r.status_code >= 400:
                logger.warning("dg tts: %s %s", r.status_code, r.text[:200])
                return b""
            return r.content
        except Exception as exc:  # noqa: BLE001 - a failed reply must not kill the call
            logger.warning("dg tts: failed: %r", exc)
            return b""

    async def synthesize_stream(self, text: str, voice: str,
                                instructions: str = "", model: str = ""):
        """Yield 8 kHz PCM as it is synthesized. No downsampler, so no chunk-boundary
        remainder to carry -- every chunk is already whole samples at the right rate."""
        text = (text or "").strip()
        if not text or not settings.DEEPGRAM_API_KEY:
            return
        params = self._params(self.resolved_model(voice, model))
        payload = {"text": text[: settings.TTS_MAX_CHARS]}
        try:
            async with httpx.AsyncClient(timeout=_TTS_TIMEOUT) as c:
                async with c.stream("POST", settings.DG_TTS_REST_URL, params=params,
                                    headers=self._headers, json=payload) as r:
                    if r.status_code >= 400:
                        body = (await r.aread())[:200]
                        logger.warning("dg tts stream: %s %s", r.status_code, body)
                        return
                    async for chunk in r.aiter_bytes():
                        if chunk:
                            yield chunk
        except Exception as exc:  # noqa: BLE001
            logger.warning("dg tts stream: failed: %r", exc)
            return


# --- streaming STT (VOICE_STACK_MIGRATION M1) -----------------------------------------------

@dataclass
class TurnEvent:
    """One turn-lifecycle event from a streaming STT.

    `kind` is deliberately our own vocabulary rather than Deepgram's, so a second streaming
    vendor (AssemblyAI, Cartesia Ink-2) maps onto the same four words without the pipeline
    learning anything about either.

        start       the caller began speaking       -> barge-in
        eager_end   PREDICTED turn end              -> draft the LLM reply, DO NOT speak
        resumed     the prediction was wrong        -> discard the draft
        end         committed turn end, final text  -> speak
        error       the stream is gone              -> fail the session
    """

    kind: str
    transcript: str = ""
    confidence: float = 0.0


class StreamingSpeechToText(Protocol):
    """The seam batch STT cannot express.

    `SpeechToText.transcribe(pcm) -> str` assumes the CALLER of the STT decides when a turn
    ended. Flux inverts that: audio streams continuously and the vendor announces the turn
    boundary along with the final transcript. That is not a faster transcribe(), it is a
    different control flow, so it gets its own protocol rather than being forced through the
    old one (M1).
    """

    name: str

    async def start(self) -> bool: ...
    async def feed(self, pcm8k: bytes) -> None: ...
    async def close(self) -> None: ...


class DeepgramFluxSTT:
    """Deepgram Flux over wss://api.deepgram.com/v2/listen (M1, M2).

    Audio goes in continuously; `events` receives TurnEvents. Two savings over the batch path,
    and they are different things: the 600 ms local VAD hangover disappears because Flux
    decides end-of-turn semantically, and the STT round trip disappears because the transcript
    is ALREADY FINAL when EndOfTurn arrives.

    EAGER MODE (M2). When DG_EAGER_EOT_THRESHOLD is set, Flux emits EagerEndOfTurn on a
    PREDICTED turn end. Deepgram's contract, which this class relies on: the EndOfTurn
    transcript exactly matches the EagerEndOfTurn transcript, so a draft prepared eagerly needs
    no reconciliation. The pipeline may draft an LLM reply on `eager_end`; it must NOT speak
    until `end`, because a `resumed` after audio began is the agent talking over a caller who
    never stopped.

    NEVER RAISES into the call path. A dead socket becomes a single `error` event and the
    session fails to voicemail (M6) -- the path the system already tests.
    """

    name = "deepgram"

    def __init__(self) -> None:
        self.events: "asyncio.Queue[TurnEvent]" = asyncio.Queue()
        self._ws = None
        self._reader: Optional[asyncio.Task] = None
        self._buf = bytearray()
        # Deepgram recommends ~80ms chunks; AudioSocket hands us 20ms frames.
        self._chunk_bytes = AUDIO_FRAME_BYTES * max(1, settings.DG_SEND_CHUNK_FRAMES)
        self._closed = False

    def _url(self) -> str:
        from urllib.parse import urlencode

        q = {
            "model": settings.DG_STT_MODEL,
            "encoding": "linear16",     # exactly what AudioSocket carries -- no resampling
            "sample_rate": "8000",
            "eot_threshold": f"{settings.DG_EOT_THRESHOLD:.2f}",
            "eot_timeout_ms": str(settings.DG_EOT_TIMEOUT_MS),
        }
        if settings.DG_EAGER_EOT_THRESHOLD > 0:
            q["eager_eot_threshold"] = f"{settings.DG_EAGER_EOT_THRESHOLD:.2f}"
        return f"{settings.DG_STT_URL}?{urlencode(q)}"

    async def start(self) -> bool:
        if not settings.DEEPGRAM_API_KEY:
            logger.warning("dg stt: no DEEPGRAM_API_KEY, refusing to start")
            return False
        try:
            import websockets

            self._ws = await websockets.connect(
                self._url(),
                extra_headers={"Authorization": f"Token {settings.DEEPGRAM_API_KEY}"},
                open_timeout=5,
                # Our own frames are the liveness signal; Deepgram closes on its own timeout.
                ping_interval=None,
                max_size=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("dg stt: connect failed: %r", exc)
            return False
        self._reader = asyncio.create_task(self._read_loop())
        logger.info("dg stt: connected model=%s eager=%s",
                    settings.DG_STT_MODEL, settings.DG_EAGER_EOT_THRESHOLD or "off")
        return True

    # Deepgram's `event` values -> our vocabulary. Anything absent (Update, and any value a
    # future model version introduces) is IGNORED rather than guessed at: an unknown turn
    # event must never be mistaken for a turn ending.
    _EVENTS = {
        "StartOfTurn": "start",
        "EagerEndOfTurn": "eager_end",
        "TurnResumed": "resumed",
        "EndOfTurn": "end",
    }

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray)):
                    continue        # Flux sends no binary; ignore rather than crash
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                mtype = msg.get("type")
                if mtype == "Error":
                    logger.warning("dg stt: fatal %s", str(msg)[:200])
                    await self.events.put(TurnEvent("error"))
                    return
                if mtype != "TurnInfo":
                    continue        # Connected / ConfigureSuccess / ...
                kind = self._EVENTS.get(str(msg.get("event") or ""))
                if kind is None:
                    continue
                # Deepgram sends confidences as strings in places; coerce defensively.
                try:
                    conf = float(msg.get("end_of_turn_confidence") or 0.0)
                except (TypeError, ValueError):
                    conf = 0.0
                await self.events.put(TurnEvent(
                    kind=kind,
                    transcript=str(msg.get("transcript") or "").strip(),
                    confidence=conf,
                ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not self._closed:
                logger.warning("dg stt: stream ended: %r", exc)
                await self.events.put(TurnEvent("error"))

    async def feed(self, pcm8k: bytes) -> None:
        """Buffer 20 ms frames and flush at the recommended chunk size. Never raises: a send
        failure surfaces through the read loop's `error` event, not here in the audio path."""
        if self._ws is None or self._closed or not pcm8k:
            return
        self._buf.extend(pcm8k)
        if len(self._buf) < self._chunk_bytes:
            return
        chunk, self._buf = bytes(self._buf), bytearray()
        try:
            await self._ws.send(chunk)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dg stt: send failed: %r", exc)

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:  # noqa: BLE001 - best effort; we are tearing down anyway
                pass
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._reader is not None:
            self._reader.cancel()


# --- selection ---------------------------------------------------------------------------------

_STT = {"openai": OpenAISTT}
_LLM = {"openai_compatible": OpenAICompatibleLLM}
_TTS = {"openai": OpenAITTS, "deepgram": DeepgramTTS}
# Streaming STT is a DIFFERENT seam, not another entry in _STT: these classes do not implement
# transcribe(). Keeping the registries apart is what stops get_stt() ever returning something
# the batch call path cannot use.
_STREAM_STT = {"deepgram": DeepgramFluxSTT}


def resolve_provider(kind: str, agent_choice: str = "") -> str:
    """Which vendor answers for this call (M4): LOCK > agent version > env default.

    The lock is the incident switch. Without it, ending a vendor outage would mean editing
    every agent that pinned that vendor -- and since agent versions are IMMUTABLE, "editing"
    means re-versioning each one, mid-incident. One env value and a restart instead.
    """
    if kind == "stt":
        return (settings.STT_PROVIDER_LOCK or str(agent_choice or "")
                or settings.STT_PROVIDER).strip()
    return (settings.TTS_PROVIDER_LOCK or str(agent_choice or "")
            or settings.TTS_PROVIDER).strip()


def streaming_stt_available(agent_choice: str = "") -> bool:
    """True when the resolved STT provider drives its own turn detection.

    The pipeline uses this to decide which loop it is running -- local VAD, or vendor EOT.
    """
    return resolve_provider("stt", agent_choice) in _STREAM_STT


def get_streaming_stt(agent_choice: str = "") -> Optional[StreamingSpeechToText]:
    cls = _STREAM_STT.get(resolve_provider("stt", agent_choice))
    return cls() if cls else None


def get_stt(agent_choice: str = "") -> SpeechToText:
    return _STT.get(resolve_provider("stt", agent_choice), OpenAISTT)()


def get_llm(base_url: str = "", model: str = "") -> LanguageModel:
    cls = _LLM.get(settings.LLM_PROVIDER, OpenAICompatibleLLM)
    return cls(base_url=base_url, model=model)


def get_tts(agent_choice: str = "") -> TextToSpeech:
    return _TTS.get(resolve_provider("tts", agent_choice), OpenAITTS)()


# --- custom tool execution (AI_AGENT_SPEC D6) -----------------------------------------------

async def call_custom_tool(tool: dict, args: dict) -> tuple[int, object]:
    """Invoke one declared HTTP tool. Returns (status, parsed_body); (0, None) on failure.

    The timeout is the tool's MODE, not a global: a sync tool has ~800ms because the caller is
    listening to silence, while an async one is fire-and-forget and its duration is nobody's
    problem. Never raises — a caller must not lose a call because a backend was slow.
    """
    import httpx

    from app.custom_tools import SYNC_BUDGET_S, resolve_headers

    timeout = SYNC_BUDGET_S if tool.get("mode") == "sync" else 20.0
    method = tool.get("method", "GET")
    kwargs: dict = {}
    if method == "GET":
        kwargs["params"] = args or None
    else:
        kwargs["json"] = args or {}
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            # Secrets are resolved from the environment HERE, never stored in the pinned
            # version the declaration came from (M14).
            r = await c.request(method, tool["url"],
                                headers=resolve_headers(tool.get("headers")) or None, **kwargs)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text[:2000]
    except Exception as exc:  # noqa: BLE001
        logger.warning("custom tool %s failed: %r", tool.get("name"), exc)
        return 0, None

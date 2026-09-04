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

import json
import logging
from typing import Optional, Protocol

import httpx

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
            "max_tokens": settings.LLM_MAX_TOKENS,
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
            "max_tokens": settings.LLM_MAX_TOKENS,
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
                        body = (await r.aread())[:200]
                        logger.warning("llm stream: %s %s", r.status_code, body)
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


# --- selection ---------------------------------------------------------------------------------

_STT = {"openai": OpenAISTT}
_LLM = {"openai_compatible": OpenAICompatibleLLM}
_TTS = {"openai": OpenAITTS}


def get_stt() -> SpeechToText:
    return _STT.get(settings.STT_PROVIDER, OpenAISTT)()


def get_llm(base_url: str = "", model: str = "") -> LanguageModel:
    cls = _LLM.get(settings.LLM_PROVIDER, OpenAICompatibleLLM)
    return cls(base_url=base_url, model=model)


def get_tts() -> TextToSpeech:
    return _TTS.get(settings.TTS_PROVIDER, OpenAITTS)()

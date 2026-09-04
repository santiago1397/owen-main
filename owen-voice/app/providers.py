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

import logging
from typing import Optional, Protocol

import httpx

from app.config import settings
from app.dsp import downsample_24k_to_8k, wav_unwrap, wav_wrap

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

    async def reply(self, system: str, history: list[dict]) -> str:
        if not settings.LLM_API_KEY:
            logger.warning("llm: no API key configured")
            return ""
        messages = [{"role": "system", "content": system}] if system else []
        messages += history
        payload = {
            "model": settings.LLM_MODEL,
            "messages": messages,
            "temperature": settings.LLM_TEMPERATURE,
            # Capped hard: this is speech. A model that decides to produce five paragraphs
            # makes the caller listen to all of it, and pays for TTS on every word.
            "max_tokens": settings.LLM_MAX_TOKENS,
        }
        try:
            async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as c:
                r = await c.post(
                    f"{settings.LLM_BASE_URL}/chat/completions",
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


# --- TTS -----------------------------------------------------------------------------------

class TextToSpeech(Protocol):
    name: str

    async def synthesize(self, text: str, voice: str) -> bytes: ...


class OpenAITTS:
    """OpenAI /audio/speech -> 8 kHz PCM ready for AudioSocket.

    Asks for `wav` (24 kHz) and downsamples locally rather than shelling out to ffmpeg: this
    is the latency-critical path, and the backend's flow-TTS already shows how much operational
    weight an ffmpeg dependency carries. Returns raw 8 kHz PCM, or b"" on any failure —
    silence is recoverable, an exception here is not.
    """

    name = "openai"

    async def synthesize(self, text: str, voice: str) -> bytes:
        text = (text or "").strip()
        if not text or not settings.OPENAI_API_KEY:
            return b""
        payload = {
            "model": settings.TTS_MODEL,
            "voice": voice or settings.TTS_VOICE,
            "input": text[: settings.TTS_MAX_CHARS],
            "response_format": "wav",
        }
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


# --- selection ---------------------------------------------------------------------------------

_STT = {"openai": OpenAISTT}
_LLM = {"openai_compatible": OpenAICompatibleLLM}
_TTS = {"openai": OpenAITTS}


def get_stt() -> SpeechToText:
    return _STT.get(settings.STT_PROVIDER, OpenAISTT)()


def get_llm() -> LanguageModel:
    return _LLM.get(settings.LLM_PROVIDER, OpenAICompatibleLLM)()


def get_tts() -> TextToSpeech:
    return _TTS.get(settings.TTS_PROVIDER, OpenAITTS)()

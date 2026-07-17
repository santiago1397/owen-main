"""Pluggable transcription engines (ARCHITECTURE.md #4).

The rest of the app only sees `Transcript`; which engine produced it is a config switch,
so "local light model vs cloud API" never leaks past this module. `dummy` is the offline
default so the pipeline is fully testable without external services.
"""

from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.core.config import settings


@dataclass
class Transcript:
    text: str
    language: str | None = None
    confidence: float | None = None
    words: dict | None = field(default=None)
    # Time-ordered segments for one audio file: [{start: float, end: float, text: str}].
    # The engine transcribes a single (mono) file and has no notion of speaker — the
    # dual-channel handler assigns caller/operator per channel and merges (see handlers.py).
    segments: list | None = field(default=None)


class TranscriptionEngine(Protocol):
    name: str

    async def transcribe(self, audio_path: str) -> Transcript: ...

    async def transcribe_segmented(self, audio_path: str) -> Transcript:
        """Like `transcribe`, but the result MUST carry `segments` with start/end times
        (used by the dual-channel path to interleave the two legs). Engines that can't
        produce timestamps should fall back to a single whole-file segment."""
        ...


class DummyTranscriptionEngine:
    """Deterministic canned transcript — for local/offline runs and tests."""

    name = "dummy"

    async def transcribe(self, audio_path: str) -> Transcript:
        text = "Hello, I am calling to sell you an extended car warranty offer today."
        return Transcript(text=text, language="en", confidence=0.99)

    async def transcribe_segmented(self, audio_path: str) -> Transcript:
        text = "Hello, I am calling to sell you an extended car warranty offer today."
        return Transcript(
            text=text, language="en", confidence=0.99,
            segments=[{"start": 0.0, "end": 3.0, "text": text}],
        )


class OpenAITranscriptionEngine:
    """OpenAI transcription API. Phone audio is 8kHz/noisy — cloud is the realistic default.

    Mono transcripts use OPENAI_TRANSCRIBE_MODEL (prod: gpt-4o-transcribe — resists the
    fake-text hallucination whisper-1 produces on short/near-silent audio). But that model
    only supports response_format=json/text — no timestamps. The dual-channel path needs
    per-segment start/end times to interleave the two legs, and among OpenAI models only
    whisper-1 returns them (verbose_json). So `transcribe_segmented` uses whisper-1 and then
    filters out likely-hallucinated segments (its known weakness) via no_speech_prob /
    avg_logprob thresholds — the silent stretches of a split channel are exactly where
    whisper-1 invents text, so this filtering matters."""

    name = "openai"

    async def _post(self, audio_path: str, model: str, response_format: str) -> dict:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY not set")
        async with httpx.AsyncClient(timeout=120) as client:
            with open(audio_path, "rb") as fh:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                    files={"file": (audio_path.split("/")[-1], fh, "audio/mpeg")},
                    data={"model": model, "response_format": response_format},
                )
        resp.raise_for_status()
        return resp.json()

    async def transcribe(self, audio_path: str) -> Transcript:
        body = await self._post(audio_path, settings.OPENAI_TRANSCRIBE_MODEL, "json")
        return Transcript(text=body.get("text", ""), language="en")

    async def transcribe_segmented(self, audio_path: str) -> Transcript:
        body = await self._post(audio_path, settings.OPENAI_STEREO_TRANSCRIBE_MODEL, "verbose_json")
        segments = []
        for s in body.get("segments", []):
            text = (s.get("text") or "").strip()
            if not text:
                continue
            # Drop probable hallucinations on near-silent stretches of this leg's channel.
            if (s.get("no_speech_prob") or 0.0) > settings.STEREO_MAX_NO_SPEECH_PROB:
                continue
            if (s.get("avg_logprob") if s.get("avg_logprob") is not None else 0.0) \
                    < settings.STEREO_MIN_AVG_LOGPROB:
                continue
            segments.append({"start": s.get("start"), "end": s.get("end"), "text": text})
        # Rebuild flat text from the *filtered* segments so text and segments agree.
        return Transcript(
            text=" ".join(s["text"] for s in segments),
            language="en",
            segments=segments or None,
        )


_ENGINES = {"dummy": DummyTranscriptionEngine, "openai": OpenAITranscriptionEngine}


def get_transcription_engine() -> TranscriptionEngine:
    return _ENGINES.get(settings.TRANSCRIPTION_ENGINE, DummyTranscriptionEngine)()

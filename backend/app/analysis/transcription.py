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


class TranscriptionEngine(Protocol):
    name: str

    async def transcribe(self, audio_path: str) -> Transcript: ...


class DummyTranscriptionEngine:
    """Deterministic canned transcript — for local/offline runs and tests."""

    name = "dummy"

    async def transcribe(self, audio_path: str) -> Transcript:
        return Transcript(
            text="Hello, I am calling to sell you an extended car warranty offer today.",
            language="en",
            confidence=0.99,
        )


class OpenAITranscriptionEngine:
    """OpenAI Whisper API. Phone audio is 8kHz/noisy — cloud is the realistic default."""

    name = "openai"

    async def transcribe(self, audio_path: str) -> Transcript:
        if not settings.OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY not set")
        async with httpx.AsyncClient(timeout=120) as client:
            with open(audio_path, "rb") as fh:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                    files={"file": (audio_path.split("/")[-1], fh, "audio/mpeg")},
                    data={"model": settings.OPENAI_TRANSCRIBE_MODEL, "response_format": "json"},
                )
        resp.raise_for_status()
        return Transcript(text=resp.json().get("text", ""), language="en")


_ENGINES = {"dummy": DummyTranscriptionEngine, "openai": OpenAITranscriptionEngine}


def get_transcription_engine() -> TranscriptionEngine:
    return _ENGINES.get(settings.TRANSCRIPTION_ENGINE, DummyTranscriptionEngine)()

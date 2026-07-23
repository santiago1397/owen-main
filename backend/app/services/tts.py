"""Flow-prompt TTS (Ticket 15.2) — OpenAI text-to-speech, cached as Asterisk-playable WAVs.

Flow graphs store prompts as plain TEXT ("Thanks for calling..."); Asterisk can only play
sound files. This service closes that gap:

- `synthesize(text)` calls the OpenAI speech API (`TTS_MODEL`, default tts-1; `TTS_VOICE`,
  default alloy; reuses `OPENAI_API_KEY`) and resamples the result with ffmpeg (the same
  invocation style as the stereo-split code in app/analysis/audio.py) to 8kHz mono 16-bit
  WAV — slin-compatible, so Asterisk plays it natively over a PSTN leg.
- Output is content-addressed under `<RECORDINGS_DIR>/tts/<sha256(text|voice)>.wav`, so the
  same prompt text is synthesized ONCE and reused across calls, versions and flows. The
  recordings volume is shared app↔worker (docker-compose.prod.yml) and must be readable by
  the native Asterisk host at the SAME absolute path (see asterisk/README.md) — playback
  uses an absolute-path `sound:` URI (path WITHOUT the .wav extension, per Asterisk media
  URI rules).

Synthesis triggers (both best-effort — a TTS failure must never dead-air or block anything):
- at flow ACTIVATION, `prewarm_graph_prompts` synthesizes every static prompt in the graph
  (fired-and-forgotten by the activate endpoint; failures are logged, activation proceeds);
- LAZILY at call time via `resolve_prompt` when the cached file is missing (or the text was
  never prewarmed, e.g. `{{...}}` interpolation — Phase 3). If that fails the caller skips
  playback and the flow continues.

The pure helpers (`cache_key`, `is_media_uri`, `graph_prompt_texts`) import nothing beyond
stdlib and settings/httpx are imported lazily inside the I/O functions, so the cache-keying
and prompt-extraction rules are unit-testable in the sandbox (mirrors app/agents/session.py).
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger("services.tts")

# ARI media-URI schemes. A prompt string starting with one of these is already a playable
# media reference and is passed through untouched — only bare text goes through TTS.
MEDIA_URI_SCHEMES: tuple[str, ...] = (
    "sound:", "recording:", "digits:", "number:", "characters:", "tone:", "play:"
)

# Node types whose config carries a spoken prompt, and the keys the interpreter reads for
# it, in priority order (mirrors FlowInterpreter._media / _h_voicemail).
_PROMPT_KEYS: dict[str, tuple[str, ...]] = {
    "play": ("media", "prompt"),
    "menu": ("media", "prompt"),
    "voicemail": ("media", "prompt", "greeting"),
}

_TTS_TIMEOUT_S = 60.0


# --- Pure helpers (sandbox-testable) ------------------------------------------------------

def is_media_uri(text: str) -> bool:
    """True iff `text` is already an ARI media URI (never TTS'd)."""
    return str(text).startswith(MEDIA_URI_SCHEMES)


def cache_key(text: str, voice: str) -> str:
    """Content address of a synthesized prompt: sha256 of "text|voice". Model changes are
    deliberately NOT in the key — re-keying on model would orphan every cached prompt."""
    return hashlib.sha256(f"{text}|{voice}".encode()).hexdigest()


def graph_prompt_texts(graph: dict) -> list[str]:
    """Every static TTS-able prompt string in a flow graph, de-duplicated in node order.

    Per node only the EFFECTIVE prompt is taken (the first present key, exactly what the
    interpreter will play). Skipped: media URIs (already playable) and `{{...}}` templates
    (interpolated per-call in Phase 3 — synthesized lazily at call time instead)."""
    out: list[str] = []
    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if not isinstance(nodes, dict):
        return out
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        keys = _PROMPT_KEYS.get(node.get("type"))
        if not keys:
            continue
        for key in keys:
            value = node.get(key)
            if not value or not isinstance(value, str):
                continue
            text = value.strip()
            if text and not is_media_uri(text) and "{{" not in text and text not in out:
                out.append(text)
            break  # first present key is the one the interpreter plays
    return out


# --- Synthesis + cache (I/O; settings/httpx imported lazily) ------------------------------

def cache_path(text: str, voice: str | None = None) -> Path:
    """Where the synthesized WAV for (text, voice) lives (whether or not it exists yet)."""
    from app.core.config import settings

    v = voice or settings.TTS_VOICE
    return Path(settings.RECORDINGS_DIR) / "tts" / f"{cache_key(text, v)}.wav"


async def synthesize(text: str, voice: str | None = None) -> str | None:
    """Ensure the 8kHz-mono WAV for `text` exists in the cache; return its path.

    Cache hit -> immediate return, no API call. Miss -> OpenAI speech API + ffmpeg resample,
    written atomically (tmp then os.replace) so a concurrent call never reads a torn file.
    Returns None when there is nothing to say or no API key; raises on API/ffmpeg failure
    (callers wrap — `resolve_prompt` / `prewarm_graph_prompts` degrade gracefully)."""
    text = (text or "").strip()
    if not text:
        return None

    from app.core.config import settings

    voice = voice or settings.TTS_VOICE
    final = cache_path(text, voice)
    if final.exists():
        return str(final)
    if not settings.OPENAI_API_KEY:
        logger.warning("tts: OPENAI_API_KEY not set; cannot synthesize prompt")
        return None

    import httpx

    final.parent.mkdir(parents=True, exist_ok=True)
    raw = final.with_suffix(".raw.wav")
    tmp = final.with_suffix(".tmp.wav")
    try:
        async with httpx.AsyncClient(timeout=_TTS_TIMEOUT_S) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/speech",
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                json={
                    "model": settings.TTS_MODEL,
                    "voice": voice,
                    "input": text,
                    "response_format": "wav",
                },
            )
        resp.raise_for_status()
        raw.write_bytes(resp.content)

        # Resample to what Asterisk plays natively on a PSTN leg: 8kHz mono 16-bit PCM.
        # Same ffmpeg invocation style as the stereo-split code (app/analysis/audio.py).
        from app.analysis.audio import _run

        code, _, err = await _run(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
            "-ar", "8000", "-ac", "1", "-acodec", "pcm_s16le", str(tmp),
        )
        if code != 0:
            raise RuntimeError(
                f"ffmpeg resample failed ({code}): {err.decode(errors='replace')[:200]}"
            )
        os.replace(tmp, final)
        return str(final)
    finally:
        for leftover in (raw, tmp):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass


async def resolve_prompt(text: str) -> str | None:
    """Prompt text -> the cached file's absolute path WITHOUT extension (the form an ARI
    `sound:` URI wants), synthesizing lazily on a cache miss. Never raises: on any failure
    it returns None and the caller SKIPS playback — the flow continues, never dead air."""
    try:
        path = await synthesize(text)
    except Exception:  # noqa: BLE001 - TTS is best-effort; a miss skips playback
        logger.exception("tts: lazy synthesis failed for prompt (%d chars)", len(text or ""))
        return None
    if not path:
        return None
    return path[:-4] if path.endswith(".wav") else path


async def prewarm_graph_prompts(graph: dict) -> None:
    """Synthesize every static prompt in `graph` (activation-time prewarm). Best-effort per
    prompt: each failure is logged and the rest still synthesize; never raises."""
    for text in graph_prompt_texts(graph):
        try:
            await synthesize(text)
        except Exception:  # noqa: BLE001 - prewarm must never block or fail activation
            logger.exception("tts: prewarm failed for prompt (%d chars)", len(text))

"""Dual-channel audio helpers for speaker-separated transcription.

SignalWire's stereo Start Call Recording node puts each call leg on its own channel,
so the channel IS the speaker (no AI diarization). This module:

- probes channel count (`probe_channel_count`) and splits a stereo file into two mono
  tracks (`split_stereo`) — thin async wrappers over the `ffprobe`/`ffmpeg` binaries;
- merges the two channels' segments into one speaker-labeled, time-ordered transcript
  (`merge_channels`) — a pure function, unit-tested without ffmpeg or any API.

The split/merge orchestration lives in the transcribe handler; engines stay dumb
single-file transcribers.
"""

import asyncio
import json
import logging

logger = logging.getLogger("worker.audio")

SPEAKER_LABELS = {"caller": "Caller", "operator": "Operator"}


def merge_channels(caller_segments: list, operator_segments: list) -> tuple[str, list]:
    """Interleave two channels' segments into one conversation.

    Each input segment is {start, end, text} (speaker-agnostic, as the engine returns).
    Returns (flat_text, segments): `flat_text` is the "[Caller] …\n[Operator] …" string
    stored in transcriptions.text (and fed to analysis); `segments` is the structured
    list [{speaker, start, end, text}] stored in transcriptions.segments.

    Pure function — no I/O — so it's fully unit-testable with fabricated segments.
    """
    tagged: list[dict] = []
    for speaker, segs in (("caller", caller_segments), ("operator", operator_segments)):
        for s in segs or []:
            text = (s.get("text") or "").strip()
            if not text:
                continue
            tagged.append({
                "speaker": speaker,
                "start": float(s.get("start") or 0.0),
                "end": float(s.get("end") or 0.0),
                "text": text,
            })
    # Chronological order; ties broken by end time then caller-before-operator for stability.
    tagged.sort(key=lambda s: (s["start"], s["end"], s["speaker"]))
    flat_text = "\n".join(f"[{SPEAKER_LABELS[s['speaker']]}] {s['text']}" for s in tagged)
    return flat_text, tagged


async def _run(*args: str) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout, stderr


async def probe_channel_count(path: str) -> int:
    """Number of audio channels in `path`, via ffprobe. Raises on ffprobe failure so the
    caller can fall back to the mono path (a probe failure shouldn't lose the transcript)."""
    code, out, err = await _run(
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=channels", "-of", "json", path,
    )
    if code != 0:
        raise RuntimeError(f"ffprobe failed ({code}): {err.decode(errors='replace')[:200]}")
    streams = json.loads(out or b"{}").get("streams", [])
    if not streams:
        raise RuntimeError("ffprobe reported no audio stream")
    return int(streams[0].get("channels") or 1)


async def split_stereo(path: str, ch0_path: str, ch1_path: str) -> None:
    """Extract channel 0 -> ch0_path and channel 1 -> ch1_path as mono MP3s. Raises on
    ffmpeg failure. `-y` overwrites so a retry re-deriving the splits is safe.

    Uses the `channelsplit` filter rather than `-map_channel`: the latter was removed from
    modern ffmpeg builds (7.x), where it errors with "Unrecognized option 'map_channel'"."""
    code, _, err = await _run(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
        "-filter_complex", "[0:a]channelsplit=channel_layout=stereo[left][right]",
        "-map", "[left]", ch0_path,
        "-map", "[right]", ch1_path,
    )
    if code != 0:
        raise RuntimeError(f"ffmpeg split failed ({code}): {err.decode(errors='replace')[:200]}")

"""Per-call media sessions + the UUID registry that correlates them to TCP connections.

A session is created BEFORE the externalMedia channel exists (we mint the UUID, then hand it
to ARI), so the registry is what lets an inbound TCP connection find the call it belongs to
when Asterisk sends its opening UUID frame.

Counters are not decoration. When someone reports "I heard nothing", the question is which
half failed, and these answer it without a packet capture:

    rx_frames == 0   -> Asterisk never sent audio: externalMedia or the bridge is wrong
    rx_frames >  0
      and peak == 0  -> audio arrived but is digital silence: wrong format, or not bridged
      and tx == rx   -> we echoed everything: the return path or the bridge is wrong
"""

from __future__ import annotations

import asyncio
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Dict, Optional


def new_session_uuid() -> str:
    return str(_uuid.uuid4())


@dataclass
class MediaSession:
    session_uuid: str
    # Populated as the call is assembled; any may be None if a step failed.
    call_channel_id: Optional[str] = None
    media_channel_id: Optional[str] = None
    bridge_id: Optional[str] = None
    label: str = ""
    # How this session drives the SEND direction:
    #   "echo" — write every received frame back (the real spike; a caller hears themselves)
    #   "tone" — emit a sine and ignore input, so a bridge recording proves the send path
    #            even with no human and nothing else making sound
    mode: str = "echo"
    # Bridge recording name, for the tone self-test (the send-path evidence).
    recording_name: str | None = None
    # Per-session end-of-turn override. Exists for the self-test: its audio source is a
    # continuous recorded prompt whose gaps never reach the 700ms a human's pause does, so
    # the production threshold can never fire against it. Never set on a real call.
    vad_end_frames: int | None = None
    # Per-call TTS overrides, so voices can be compared on a REAL phone call rather than on
    # laptop speakers — the document's §3 warning is that 8kHz destroys much of what premium
    # TTS charges for, and the only honest test is down the actual phone.
    tts_voice: str | None = None
    tts_instructions: str | None = None
    tts_model: str | None = None

    created_at: float = field(default_factory=time.monotonic)
    connected_at: Optional[float] = None
    closed_at: Optional[float] = None

    rx_frames: int = 0
    rx_bytes: int = 0
    tx_frames: int = 0
    tx_bytes: int = 0
    # Peak absolute sample seen, 0..32767. Distinguishes "no audio" from "silent audio".
    peak_amplitude: int = 0
    dtmf: str = ""
    error: Optional[str] = None

    # --- conversation (step 2) ---
    turns: int = 0
    last_turn_ms: int = 0
    # What the CALLER experienced: silence between them stopping and hearing the first
    # syllable. `last_turn_ms` only records when synthesis finished, which nobody hears.
    last_first_audio_ms: int = 0
    # What the VAD actually sees. Without this, "the agent never answered" is a guess
    # between a wrong threshold, a silent stream and a stream that is never silent.
    rms_min: float = 1e9
    rms_max: float = 0.0
    rms_sum: float = 0.0
    rms_n: int = 0
    vad_starts: int = 0
    max_quiet_run: int = 0
    vad_ends: int = 0
    barge_suppressed: int = 0
    half_duplex_dropped: int = 0
    # None = use the global default; set per call to A/B it.
    half_duplex: bool | None = None
    noise_utterances: int = 0
    # Speaker-labelled, the shape the backend's `transcriptions.segments` already uses, so
    # persisting it in step 3 is a write rather than a translation.
    transcript: list = field(default_factory=list)

    _writer: Optional[asyncio.StreamWriter] = field(default=None, repr=False)

    @property
    def connected(self) -> bool:
        return self.connected_at is not None and self.closed_at is None

    @property
    def duration_s(self) -> float:
        end = self.closed_at if self.closed_at is not None else time.monotonic()
        return round(end - self.created_at, 2)

    def snapshot(self) -> dict:
        """JSON-able state for the control API — the evidence that the transport worked."""
        return {
            "session_uuid": self.session_uuid,
            "label": self.label,
            "mode": self.mode,
            "call_channel_id": self.call_channel_id,
            "media_channel_id": self.media_channel_id,
            "bridge_id": self.bridge_id,
            "connected": self.connected,
            "duration_s": self.duration_s,
            "rx_frames": self.rx_frames,
            "rx_bytes": self.rx_bytes,
            "tx_frames": self.tx_frames,
            "tx_bytes": self.tx_bytes,
            "peak_amplitude": self.peak_amplitude,
            "dtmf": self.dtmf,
            "turns": self.turns,
            "rms_min": round(self.rms_min, 1) if self.rms_n else None,
            "rms_max": round(self.rms_max, 1) if self.rms_n else None,
            "rms_avg": round(self.rms_sum / self.rms_n, 1) if self.rms_n else None,
            "vad_starts": self.vad_starts,
            "max_quiet_run": self.max_quiet_run,
            "vad_ends": self.vad_ends,
            "barge_suppressed": self.barge_suppressed,
            "half_duplex_dropped": self.half_duplex_dropped,
            "noise_utterances": self.noise_utterances,
            "last_turn_ms": self.last_turn_ms,
            "last_first_audio_ms": self.last_first_audio_ms,
            "transcript": self.transcript,
            "error": self.error,
            "verdict": self.verdict(),
        }

    def verdict(self) -> str:
        """A one-line reading of the counters, so the result of the spike is unambiguous."""
        if self.error:
            return f"error: {self.error}"
        if self.connected_at is None:
            return "asterisk never connected — check externalMedia + advertise host/port"
        if self.mode == "agent":
            if self.rx_frames == 0:
                return "connected but no audio received — channel likely not bridged"
            if self.turns == 0:
                return "audio flowed but no complete turn — check VAD threshold / STT key"
            return f"OK — {self.turns} conversational turn(s)"
        if self.mode == "tone":
            # A tone session deliberately ignores input, so rx says nothing about success —
            # the proof is that we sent, and that Asterisk's bridge recording is non-silent.
            if self.tx_frames == 0:
                return "no tone frames sent — send path failed inside owen-voice"
            return (
                f"SENT {self.tx_frames} tone frames "
                "— confirm the bridge recording is non-silent to prove the return path"
            )
        if self.rx_frames == 0:
            return "connected but no audio received — channel likely not bridged"
        if self.peak_amplitude == 0:
            return "audio received but digital silence — check media format / bridge"
        if self.tx_frames == 0:
            return "audio received but nothing echoed back — write path failed"
        return "OK — audio flowed both ways"


def peak_of(pcm: bytes, current: int) -> int:
    """Highest absolute 16-bit LE sample in this frame, vs the running peak. Cheap enough for
    the hot path (one pass, no allocation beyond ints) and it is the single most useful signal
    for telling a dead transport apart from a silent one."""
    peak = current
    for i in range(0, len(pcm) - 1, 2):
        s = int.from_bytes(pcm[i:i + 2], "little", signed=True)
        a = -s if s < 0 else s
        if a > peak:
            peak = a
    return peak


class SessionRegistry:
    """UUID -> MediaSession. Small, in-memory, per-process — a session's lifetime is one call,
    and a restart drops the RTP anyway, so there is nothing worth persisting."""

    def __init__(self) -> None:
        self._sessions: Dict[str, MediaSession] = {}

    def create(self, label: str = "") -> MediaSession:
        s = MediaSession(session_uuid=new_session_uuid(), label=label)
        self._sessions[s.session_uuid] = s
        return s

    def get(self, session_uuid: str) -> Optional[MediaSession]:
        return self._sessions.get(session_uuid)

    def all(self) -> list[MediaSession]:
        return list(self._sessions.values())

    def active(self) -> list[MediaSession]:
        return [s for s in self._sessions.values() if s.connected]

    def drop(self, session_uuid: str) -> None:
        self._sessions.pop(session_uuid, None)

    def prune(self, keep_last: int = 20) -> None:
        """Keep the registry bounded: closed sessions are retained only so their counters can
        be read after the fact."""
        closed = sorted(
            (s for s in self._sessions.values() if s.closed_at is not None),
            key=lambda s: s.closed_at or 0,
        )
        for s in closed[:-keep_last] if len(closed) > keep_last else []:
            self._sessions.pop(s.session_uuid, None)


registry = SessionRegistry()

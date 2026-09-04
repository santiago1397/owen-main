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
            "error": self.error,
            "verdict": self.verdict(),
        }

    def verdict(self) -> str:
        """A one-line reading of the counters, so the result of the spike is unambiguous."""
        if self.error:
            return f"error: {self.error}"
        if self.connected_at is None:
            return "asterisk never connected — check externalMedia + advertise host/port"
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

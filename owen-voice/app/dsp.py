"""Signal processing — PURE, stdlib only, no I/O (step 2).

Turn detection, resampling and WAV framing all live here so they are testable with no
Asterisk, no vendor and no event loop — the same split that keeps the backend's flow
interpreter and billing kernel honest.

WHY A LOCAL VAD AT ALL: the spec's recommended STT (Deepgram Flux) has end-of-turn detection
built in, which is most of why it was chosen. Running against OpenAI instead means turn
detection is ours to do. This is deliberately a plain energy VAD with hangover — it is not as
good as a trained model, and it is the piece to delete rather than improve when Flux lands.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Optional

from app.audiosocket import AUDIO_FRAME_BYTES, SAMPLE_RATE_HZ

# --- level ---------------------------------------------------------------------------------


def rms_of(pcm: bytes) -> float:
    """Root-mean-square level of a 16-bit LE mono frame, 0..32767."""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    total = 0
    for i in range(0, n * 2, 2):
        s = int.from_bytes(pcm[i:i + 2], "little", signed=True)
        total += s * s
    return math.sqrt(total / n)


# --- turn detection --------------------------------------------------------------------------

# Telephony noise floor sits well under this; normal speech runs several times above it.
DEFAULT_SPEECH_RMS = 700.0
# Frames of speech before we believe it (3 x 20ms = 60ms). Rejects clicks and codec artefacts.
DEFAULT_START_FRAMES = 3
# Frames of silence that end a turn (35 x 20ms = 700ms). Long enough to survive the pause
# inside a sentence, short enough not to feel laggy.
DEFAULT_END_FRAMES = 35
# Utterances shorter than this are discarded rather than sent to STT — a cough is not a turn,
# and every STT call costs money and latency.
DEFAULT_MIN_SPEECH_FRAMES = 10  # 200ms


@dataclass
class TurnDetector:
    """Energy VAD with hangover. Feed frames; it tells you when a turn starts and ends.

    `push` returns one of:
        None                 nothing happened
        ("start", None)      speech began — the caller is talking (used for BARGE-IN)
        ("end", pcm_bytes)   speech ended — `pcm_bytes` is the whole utterance
    """

    speech_rms: float = DEFAULT_SPEECH_RMS
    start_frames: int = DEFAULT_START_FRAMES
    end_frames: int = DEFAULT_END_FRAMES
    min_speech_frames: int = DEFAULT_MIN_SPEECH_FRAMES
    # Pre-roll kept before the trigger so the first syllable is not clipped. Without this the
    # caller's opening consonant is missing and STT mis-hears the first word.
    preroll_frames: int = 5

    _in_speech: bool = field(default=False, init=False)
    _loud_run: int = field(default=0, init=False)
    _quiet_run: int = field(default=0, init=False)
    _buf: list = field(default_factory=list, init=False)
    _preroll: list = field(default_factory=list, init=False)
    _speech_frames: int = field(default=0, init=False)
    # Longest run of consecutive sub-threshold frames seen while in speech. If a turn never
    # ends, this is the number that says whether the stream is never quiet or the threshold
    # is simply wrong — the two indistinguishable causes of "the agent never answered".
    max_quiet_run: int = field(default=0, init=False)

    def push(self, pcm: bytes) -> Optional[tuple[str, Optional[bytes]]]:
        loud = rms_of(pcm) >= self.speech_rms

        if not self._in_speech:
            self._preroll.append(pcm)
            if len(self._preroll) > self.preroll_frames:
                self._preroll.pop(0)
            self._loud_run = self._loud_run + 1 if loud else 0
            if self._loud_run >= self.start_frames:
                self._in_speech = True
                self._quiet_run = 0
                self._buf = list(self._preroll)
                self._speech_frames = self._loud_run
                self._preroll = []
                return ("start", None)
            return None

        self._buf.append(pcm)
        if loud:
            self._speech_frames += 1
            self._quiet_run = 0
            return None

        self._quiet_run += 1
        self.max_quiet_run = max(self.max_quiet_run, self._quiet_run)
        if self._quiet_run < self.end_frames:
            return None

        # Turn over.
        audio = b"".join(self._buf)
        speech = self._speech_frames
        self._in_speech = False
        self._loud_run = 0
        self._quiet_run = 0
        self._buf = []
        self._speech_frames = 0
        if speech < self.min_speech_frames:
            return None  # too short to be a turn; stay quiet rather than call STT on a cough
        return ("end", audio)

    def reset(self) -> None:
        self._in_speech = False
        self._loud_run = self._quiet_run = self._speech_frames = 0
        self._buf = []
        self._preroll = []


# --- resampling ------------------------------------------------------------------------------


def downsample_24k_to_8k(pcm24: bytes) -> bytes:
    """24 kHz -> 8 kHz by averaging each group of 3 samples.

    OpenAI's TTS emits 24 kHz PCM and the phone network is 8 kHz. Averaging is a crude
    low-pass before decimating: plain every-third-sample decimation aliases anything above
    4 kHz back down into the speech band, which sounds like a metallic rasp. ffmpeg would do
    this better, but it is a subprocess per utterance on a latency-critical path, and the
    backend already proves how annoying that dependency is to keep working."""
    out = bytearray()
    n = len(pcm24) // 2
    samples = struct.unpack("<%dh" % n, pcm24[: n * 2]) if n else ()
    for i in range(0, n - 2, 3):
        out += int((samples[i] + samples[i + 1] + samples[i + 2]) // 3).to_bytes(
            2, "little", signed=True
        )
    return bytes(out)


def chunk_frames(pcm: bytes, size: int = AUDIO_FRAME_BYTES) -> list[bytes]:
    """Split PCM into AudioSocket-sized frames, zero-padding the tail so the last frame is a
    full 20 ms — a short frame at the end is a click."""
    out = []
    for i in range(0, len(pcm), size):
        f = pcm[i:i + size]
        if len(f) < size:
            f = f + b"\x00" * (size - len(f))
        out.append(f)
    return out


# --- WAV ---------------------------------------------------------------------------------------


def wav_wrap(pcm: bytes, *, rate: int = SAMPLE_RATE_HZ, channels: int = 1) -> bytes:
    """Wrap raw PCM in a WAV container. STT endpoints want a real file, not bare samples."""
    byte_rate = rate * channels * 2
    return (
        b"RIFF" + (36 + len(pcm)).to_bytes(4, "little") + b"WAVE"
        + b"fmt " + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
        + channels.to_bytes(2, "little") + rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little") + (channels * 2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data" + len(pcm).to_bytes(4, "little") + pcm
    )


def wav_unwrap(data: bytes) -> bytes:
    """Raw PCM out of a WAV, by locating the `data` chunk rather than assuming a 44-byte
    header — vendors emit extra chunks (LIST/fact) and a fixed offset yields noise."""
    if not data.startswith(b"RIFF") or len(data) < 12:
        return data  # already raw
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        if cid == b"data":
            return data[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    return b""

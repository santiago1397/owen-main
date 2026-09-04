"""Pure tests for turn detection, resampling and WAV framing (step 2).

These are the pieces that decide whether a conversation feels right, and every one of them
fails silently when wrong: a bad VAD threshold means the agent never answers, a bad resample
means it sounds like a robot, a bad WAV header means STT returns empty and the call looks
mute. None of that raises an exception, so it has to be tested here rather than discovered
on a call.

Run:  python -m tests.test_dsp      (from owen-voice/)
"""

import math
import struct
import sys

sys.path.insert(0, ".")

from app.audiosocket import AUDIO_FRAME_BYTES  # noqa: E402
from app.dsp import (  # noqa: E402
    TurnDetector,
    chunk_frames,
    downsample_24k_to_8k,
    rms_of,
    wav_unwrap,
    wav_wrap,
)

_checks = 0


def check(cond, label):
    global _checks
    _checks += 1
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    print(f"  ok  {label}")


def frame(amplitude: int, samples: int = 160) -> bytes:
    """One 20 ms frame of a constant-amplitude square-ish signal."""
    return struct.pack("<%dh" % samples, *([amplitude, -amplitude] * (samples // 2)))


SILENCE = frame(0)
SPEECH = frame(4000)


def test_rms():
    check(rms_of(SILENCE) == 0.0, "silence has zero RMS")
    check(abs(rms_of(SPEECH) - 4000) < 1, "constant 4000 amplitude gives RMS 4000")
    check(rms_of(b"") == 0.0, "empty frame does not divide by zero")


def test_turn_start_needs_sustained_speech():
    """A single loud frame is a click, not a turn."""
    d = TurnDetector()
    check(d.push(SPEECH) is None, "one loud frame does not start a turn")
    check(d.push(SPEECH) is None, "two loud frames do not start a turn")
    ev = d.push(SPEECH)
    check(ev is not None and ev[0] == "start", "three loud frames start a turn")


def test_turn_ends_after_silence_and_returns_audio():
    d = TurnDetector(end_frames=5, min_speech_frames=3)
    for _ in range(3):
        d.push(SPEECH)
    for _ in range(10):
        d.push(SPEECH)
    ev = None
    for _ in range(5):
        ev = d.push(SILENCE)
    check(ev is not None and ev[0] == "end", "sustained silence ends the turn")
    audio = ev[1]
    check(isinstance(audio, bytes) and len(audio) > 0, "the utterance audio comes back")
    check(len(audio) % 2 == 0, "utterance is whole 16-bit samples")


def test_brief_pause_does_not_end_a_turn():
    """The pause inside a sentence must not be mistaken for the end of a turn."""
    d = TurnDetector(end_frames=35)
    for _ in range(3):
        d.push(SPEECH)
    ended = False
    for _ in range(20):          # 400ms of silence, mid-sentence
        if d.push(SILENCE) is not None:
            ended = True
    check(not ended, "400ms of silence does not end a 700ms-threshold turn")
    check(d.push(SPEECH) is None, "speech resumes inside the same turn")


def test_cough_is_discarded():
    """Short noise must not cost an STT call."""
    d = TurnDetector(end_frames=5, min_speech_frames=20)
    for _ in range(3):
        d.push(SPEECH)
    ev = None
    for _ in range(5):
        ev = d.push(SILENCE)
    check(ev is None, "an utterance below min_speech_frames yields no turn")


def test_preroll_keeps_the_first_syllable():
    """Speech only becomes 'speech' after 3 frames — without pre-roll those are lost and the
    caller's first consonant is missing."""
    d = TurnDetector(end_frames=3, min_speech_frames=1, preroll_frames=5)
    for _ in range(3):
        d.push(SPEECH)
    d.push(SPEECH)
    ev = None
    for _ in range(3):
        ev = d.push(SILENCE)
    check(ev is not None and ev[0] == "end", "turn ended")
    frames = len(ev[1]) // AUDIO_FRAME_BYTES
    check(frames > 4, f"utterance includes pre-roll ({frames} frames > the 4 after trigger)")


def test_downsample_preserves_a_tone():
    """A 440 Hz tone at 24k must still be 440 Hz at 8k — the check that catches aliasing."""
    sr24 = 24000
    n = sr24  # one second
    pcm24 = struct.pack(
        "<%dh" % n, *[int(8000 * math.sin(2 * math.pi * 440 * i / sr24)) for i in range(n)]
    )
    pcm8 = downsample_24k_to_8k(pcm24)
    check(abs(len(pcm8) // 2 - 8000) <= 2, "one second at 24k becomes one second at 8k")
    s = struct.unpack("<%dh" % (len(pcm8) // 2), pcm8)
    zc = sum(1 for i in range(1, len(s)) if (s[i - 1] < 0) != (s[i] < 0))
    freq = zc * 8000 / (2 * len(s))
    check(abs(freq - 440) < 15, f"tone survives the resample ({freq:.0f} Hz vs 440)")
    check(max(abs(x) for x in s) > 6000, "amplitude is preserved, not crushed")


def test_downsample_handles_ragged_input():
    check(downsample_24k_to_8k(b"") == b"", "empty input is safe")
    check(len(downsample_24k_to_8k(frame(1000))) > 0, "a non-multiple-of-3 length is safe")


def test_chunk_frames_pads_the_tail():
    """A short final frame is an audible click."""
    out = chunk_frames(b"\x01\x02" * 500)   # 1000 bytes -> 3.125 frames
    check(len(out) == 4, "1000 bytes becomes 4 frames")
    check(all(len(f) == AUDIO_FRAME_BYTES for f in out), "every frame is exactly 320 bytes")
    check(out[-1].endswith(b"\x00" * 10), "the tail is zero-padded, not truncated")


def test_wav_roundtrip():
    pcm = b"\x11\x22" * 800
    w = wav_wrap(pcm)
    check(w.startswith(b"RIFF") and w[8:12] == b"WAVE", "produces a RIFF/WAVE header")
    check(int.from_bytes(w[24:28], "little") == 8000, "sample rate is 8 kHz")
    check(int.from_bytes(w[22:24], "little") == 1, "mono")
    check(wav_unwrap(w) == pcm, "unwrap returns the original samples")


def test_wav_unwrap_finds_data_after_extra_chunks():
    """Vendors emit LIST/fact chunks; assuming a 44-byte header yields noise."""
    pcm = b"\xAB\xCD" * 100
    base = wav_wrap(pcm)
    extra = b"LIST" + (4).to_bytes(4, "little") + b"INFO"
    doctored = base[:12] + extra + base[12:]
    check(wav_unwrap(doctored) == pcm, "data chunk located past an unexpected chunk")
    check(wav_unwrap(pcm) == pcm, "raw PCM passes through untouched")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"\n{t.__name__}")
        t()
    print(f"\n{_checks} checks passed across {len(tests)} tests.")

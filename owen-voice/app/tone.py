"""Test-tone generation — PURE, no I/O.

Used by the phone-free self-test (`POST /spike/loopback`). Proving the SEND direction needs
audio that provably originated here: if owen-voice is the only thing making sound in a bridge
and the bridge recording is non-silent, the return path works. A recognisable sine is easier
to reason about than echoed noise, and being pure means the generator itself is testable.

Format matches AudioSocket exactly: 8 kHz, 16-bit signed linear, mono, little-endian.
"""

from __future__ import annotations

import math

from app.audiosocket import AUDIO_FRAME_BYTES, SAMPLE_RATE_HZ

DEFAULT_HZ = 440.0
# ~0.25 of full scale: unmistakably non-silent, nowhere near clipping.
DEFAULT_AMPLITUDE = 8000


def sine_frame(phase: float, *, freq_hz: float = DEFAULT_HZ,
               amplitude: int = DEFAULT_AMPLITUDE) -> tuple[bytes, float]:
    """One 20 ms frame of a sine wave, plus the phase to pass to the next call.

    Phase is carried across frames rather than restarted: a discontinuity every 20 ms would
    put a buzz on the tone and make a clean signal look like a broken one.
    """
    samples = AUDIO_FRAME_BYTES // 2
    step = 2.0 * math.pi * freq_hz / SAMPLE_RATE_HZ
    out = bytearray(AUDIO_FRAME_BYTES)
    for i in range(samples):
        value = int(amplitude * math.sin(phase))
        out[i * 2:i * 2 + 2] = value.to_bytes(2, "little", signed=True)
        phase += step
    # Keep phase bounded so a long tone cannot drift into float imprecision.
    return bytes(out), math.fmod(phase, 2.0 * math.pi)

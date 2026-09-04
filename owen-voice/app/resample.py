"""Proper 24 kHz -> 8 kHz decimation with a real anti-aliasing filter.

WHY THIS EXISTS: the first implementation averaged groups of 3 samples. That is a 3-tap box
filter, and its response at 4 kHz — the very first frequency that folds back into the speech
band when you decimate to 8 kHz — is **-3.5 dB**. Effectively unfiltered. Everything from
4-8 kHz in the TTS output was mirroring down on top of the speech, which is precisely what
"robotic" and "metallic" sound like. Reported on a live call, and it was right.

A 63-tap Hamming-windowed sinc at 3.4 kHz gives **-43 dB** at 4 kHz and better than -60 dB
above 5 kHz, while staying flat to 3 kHz — the whole band a phone carries anyway.

    frequency     box      FIR
      3000 Hz    -0.9    -0.6      (passband: both fine)
      4000 Hz    -3.5   -43.1      <- the one that matters
      6000 Hz    -9.5   -63.1

numpy does the convolution, because 63 taps over 120k samples per reply is ~7.5M
multiply-adds — fine in C, several seconds in pure Python on a latency-critical path. The
box filter is kept as a fallback so a numpy-less environment degrades in quality rather
than failing.
"""

from __future__ import annotations

import math

DECIM = 3           # 24000 / 8000
CUTOFF_HZ = 3400.0  # just under the 4 kHz fold point; phones carry ~300-3400 Hz anyway
IN_RATE = 24000.0
TAPS = 63           # odd, so the filter has an exact integer group delay

try:
    import numpy as _np
    HAVE_NUMPY = True
except Exception:  # noqa: BLE001 - fall back to the box filter rather than failing
    _np = None
    HAVE_NUMPY = False


def _design(n: int = TAPS, fc: float = CUTOFF_HZ, fs: float = IN_RATE) -> list:
    """Hamming-windowed sinc lowpass, normalised to unity DC gain."""
    m = (n - 1) / 2
    k = []
    for i in range(n):
        x = i - m
        h = 2 * fc / fs if x == 0 else math.sin(2 * math.pi * fc * x / fs) / (math.pi * x)
        w = 0.54 - 0.46 * math.cos(2 * math.pi * i / (n - 1))
        k.append(h * w)
    total = sum(k)
    return [v / total for v in k]


_KERNEL_LIST = _design()
_KERNEL = _np.array(_KERNEL_LIST, dtype=_np.float32) if HAVE_NUMPY else None


class Decimator:
    """Streaming 24k -> 8k. One instance per synthesis; feed arbitrary byte counts.

    Carries BOTH pieces of state a streamed decimation needs:
      - `_tail`: the last TAPS-1 input samples, so the filter is continuous across chunk
        boundaries (without it every boundary gets a transient — a tick at the chunk rate);
      - `_phase`: which sample of each group of 3 the next output falls on, so the output
        rate stays exactly 8 kHz instead of drifting a sample per chunk.
    """

    def __init__(self) -> None:
        self._phase = 0
        # A streamed chunk is an arbitrary BYTE count, so it can split a 16-bit sample down
        # the middle. Interpreting an odd-length buffer as int16 raises outright, which is
        # how TTS streaming silently fell back to one flush frame per reply.
        self._byte_rem = b""
        if HAVE_NUMPY:
            self._tail = _np.zeros(TAPS - 1, dtype=_np.float32)
        else:
            self._rem = b""

    def feed(self, pcm24: bytes) -> bytes:
        data = self._byte_rem + pcm24
        if len(data) % 2:
            self._byte_rem = data[-1:]   # carry the half sample into the next chunk
            data = data[:-1]
        else:
            self._byte_rem = b""
        if not data:
            return b""
        if not HAVE_NUMPY:
            return self._feed_box(data)

        x = _np.frombuffer(data, dtype="<i2").astype(_np.float32)
        buf = _np.concatenate([self._tail, x])
        if buf.size < TAPS:
            self._tail = buf
            return b""
        y = _np.convolve(buf, _KERNEL, mode="valid")
        idx = _np.arange(self._phase, y.size, DECIM)
        out = y[idx]
        self._phase = (self._phase - y.size) % DECIM
        self._tail = buf[-(TAPS - 1):]
        return _np.clip(out, -32768, 32767).astype("<i2").tobytes()

    def flush(self) -> bytes:
        """Drain the filter's tail so the final syllable is not cut off."""
        if not HAVE_NUMPY:
            rem, self._rem = self._rem, b""
            return _box(rem + bytes((-len(rem)) % (DECIM * 2)))
        pad = _np.zeros(TAPS - 1, dtype=_np.float32)
        buf = _np.concatenate([self._tail, pad])
        self._tail = _np.zeros(TAPS - 1, dtype=_np.float32)
        if buf.size < TAPS:
            return b""
        y = _np.convolve(buf, _KERNEL, mode="valid")
        idx = _np.arange(self._phase, y.size, DECIM)
        self._phase = 0
        return _np.clip(y[idx], -32768, 32767).astype("<i2").tobytes()

    # --- numpy-less fallback ---

    def _feed_box(self, pcm24: bytes) -> bytes:
        buf = self._rem + pcm24
        groups = (len(buf) // 2) // DECIM
        used = groups * DECIM * 2
        self._rem = buf[used:]
        return _box(buf[:used]) if used else b""


def _box(pcm24: bytes) -> bytes:
    """The original 3-sample average. Poor anti-aliasing (-3.5 dB at 4 kHz) — fallback only."""
    import struct

    out = bytearray()
    n = len(pcm24) // 2
    if not n:
        return b""
    s = struct.unpack("<%dh" % n, pcm24[: n * 2])
    for i in range(0, n - 2, DECIM):
        out += int((s[i] + s[i + 1] + s[i + 2]) // DECIM).to_bytes(2, "little", signed=True)
    return bytes(out)


def decimate(pcm24: bytes) -> bytes:
    """One-shot 24k -> 8k for a complete buffer (the non-streaming TTS path)."""
    d = Decimator()
    return d.feed(pcm24) + d.flush()

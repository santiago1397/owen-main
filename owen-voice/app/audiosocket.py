"""AudioSocket wire protocol — PURE codec, no I/O (Step 1, AI_AGENT_SPEC D3).

Kept stdlib-only and side-effect free for the same reason app/flows/interpreter.py and
app/services/billing.py are in the backend: the framing is then unit-testable with no
Asterisk, no socket and no event loop. Everything that touches a socket lives in server.py.

WIRE FORMAT — each frame is:

    +--------+------------------+------------------------+
    | type   | length (uint16)  | payload (length bytes) |
    | 1 byte | big-endian       |                        |
    +--------+------------------+------------------------+

Asterisk is the TCP *client*: it connects out to us (see `connection_type=client` on the
ARI externalMedia call) and sends a UUID frame first, which is how a connection is
correlated to the call that spawned it. Audio is 8 kHz, 16-bit signed linear, mono —
320-byte payloads (20 ms) in practice, which is also what `slin` gives us and what STT
providers accept directly, so there is no transcode in the hot path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

# --- frame types --------------------------------------------------------------------------
# TERMINATE / UUID / AUDIO / ERROR are the four this spike depends on and are stable.
KIND_TERMINATE = 0x00
KIND_UUID = 0x01
KIND_AUDIO = 0x10
KIND_ERROR = 0xFF

# DTMF's exact type byte is reported inconsistently across Asterisk versions and is NOT
# needed for the echo spike. The parser is deliberately permissive about unknown types
# (see `Frame.is_known`), so nothing breaks either way — confirm against the running
# Asterisk before relying on it for the `menu`/gather path.
KIND_DTMF_CANDIDATES = (0x02, 0x03)

HEADER_LEN = 3
MAX_PAYLOAD = 0xFFFF

# 8 kHz, 16-bit signed linear, mono. One 20 ms frame = 8000 * 0.02 * 2 bytes.
SAMPLE_RATE_HZ = 8000
BYTES_PER_SAMPLE = 2
FRAME_MS = 20
AUDIO_FRAME_BYTES = SAMPLE_RATE_HZ * BYTES_PER_SAMPLE * FRAME_MS // 1000  # 320


@dataclass(frozen=True)
class Frame:
    kind: int
    payload: bytes

    @property
    def is_audio(self) -> bool:
        return self.kind == KIND_AUDIO

    @property
    def is_terminate(self) -> bool:
        return self.kind == KIND_TERMINATE

    @property
    def is_uuid(self) -> bool:
        return self.kind == KIND_UUID

    @property
    def is_error(self) -> bool:
        return self.kind == KIND_ERROR

    @property
    def is_dtmf(self) -> bool:
        return self.kind in KIND_DTMF_CANDIDATES

    @property
    def is_known(self) -> bool:
        return (
            self.kind in (KIND_TERMINATE, KIND_UUID, KIND_AUDIO, KIND_ERROR)
            or self.is_dtmf
        )

    def uuid_str(self) -> Optional[str]:
        """The 16-byte UUID payload rendered canonically, or None if this isn't a well-formed
        UUID frame. Asterisk sends the value we passed as ARI externalMedia's `data` param —
        that is the ONLY correlation between a TCP connection and the call it belongs to."""
        if not self.is_uuid or len(self.payload) != 16:
            return None
        h = self.payload.hex()
        return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def encode(kind: int, payload: bytes = b"") -> bytes:
    """One frame on the wire. Raises on an over-long payload rather than silently truncating —
    a truncated audio frame is a click in someone's ear, and silence about it is worse."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload {len(payload)} exceeds AudioSocket max {MAX_PAYLOAD}")
    return bytes([kind & 0xFF]) + len(payload).to_bytes(2, "big") + payload


def encode_audio(pcm: bytes) -> bytes:
    return encode(KIND_AUDIO, pcm)


def encode_terminate() -> bytes:
    return encode(KIND_TERMINATE)


class FrameParser:
    """Incremental parser over a TCP byte stream.

    TCP gives no framing guarantees: one `recv` may hold three frames, half a frame, or a
    header split across two reads. Feeding bytes in and pulling whole frames out is the only
    correct shape, and getting it wrong shows up as intermittent audio corruption that is
    miserable to debug later — hence a parser with its own tests rather than inline slicing.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> Iterator[Frame]:
        """Append bytes and yield every COMPLETE frame now available. Partial trailing data
        stays buffered for the next call."""
        self._buf.extend(data)
        while True:
            if len(self._buf) < HEADER_LEN:
                return
            length = int.from_bytes(self._buf[1:3], "big")
            end = HEADER_LEN + length
            if len(self._buf) < end:
                return  # payload not fully arrived yet
            kind = self._buf[0]
            payload = bytes(self._buf[HEADER_LEN:end])
            del self._buf[:end]
            yield Frame(kind=kind, payload=payload)

    @property
    def buffered(self) -> int:
        """Bytes held pending a complete frame. Should hover near zero; a number that only
        grows means we are misreading the stream."""
        return len(self._buf)

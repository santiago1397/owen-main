"""Pure tests for the AudioSocket codec — no Asterisk, no socket, no event loop.

The framing is the one piece of this service that must be exactly right and is invisible
when it is wrong: a mis-split frame is a click in someone's ear, not an exception. So it is
pure and tested, in the same spirit as the backend's flow interpreter and billing kernel.

Run:  python -m tests.test_audiosocket      (from owen-voice/)
"""

import sys
import uuid as _uuid

sys.path.insert(0, ".")

from app.audiosocket import (  # noqa: E402
    AUDIO_FRAME_BYTES,
    KIND_AUDIO,
    KIND_TERMINATE,
    KIND_UUID,
    FrameParser,
    encode,
    encode_audio,
)

_checks = 0


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    print(f"  ok  {label}")


def test_roundtrip_single_frame():
    pcm = b"\x01\x02" * 160  # one 20ms frame
    wire = encode_audio(pcm)
    check(len(wire) == 3 + AUDIO_FRAME_BYTES, "encoded frame is header + 320 bytes")
    check(wire[0] == KIND_AUDIO, "type byte is audio")
    check(int.from_bytes(wire[1:3], "big") == AUDIO_FRAME_BYTES, "length is big-endian 320")

    frames = list(FrameParser().feed(wire))
    check(len(frames) == 1, "one frame parsed back")
    check(frames[0].payload == pcm, "payload survives the round trip")
    check(frames[0].is_audio, "parsed frame reports as audio")


def test_multiple_frames_in_one_read():
    """The kernel does not hand us one frame per read."""
    blob = encode_audio(b"\xAA\xBB" * 160) + encode_audio(b"\xCC\xDD" * 160) + encode(KIND_TERMINATE)
    frames = list(FrameParser().feed(blob))
    check(len(frames) == 3, "three frames from one read")
    check(frames[0].is_audio and frames[1].is_audio, "first two are audio")
    check(frames[2].is_terminate, "third is terminate")


def test_frame_split_across_reads():
    """The failure mode that matters: a header or payload arriving in pieces."""
    wire = encode_audio(b"\x11\x22" * 160)
    p = FrameParser()

    check(list(p.feed(wire[:1])) == [], "no frame from a partial header")
    check(list(p.feed(wire[1:3])) == [], "no frame from a complete header alone")
    check(list(p.feed(wire[3:100])) == [], "no frame from a partial payload")
    out = list(p.feed(wire[100:]))
    check(len(out) == 1, "frame emitted once the last byte arrives")
    check(out[0].payload == b"\x11\x22" * 160, "reassembled payload is byte-exact")
    check(p.buffered == 0, "nothing left buffered")


def test_byte_at_a_time():
    """Pathological but decisive: feeding one byte at a time must still yield exactly one frame."""
    wire = encode_audio(b"\x7F\x00" * 160)
    p = FrameParser()
    frames = []
    for b in wire:
        frames.extend(p.feed(bytes([b])))
    check(len(frames) == 1, "one frame from byte-at-a-time delivery")
    check(p.buffered == 0, "buffer drained")


def test_uuid_frame_correlation():
    """The UUID frame is the ONLY link between a TCP connection and its call."""
    sid = _uuid.uuid4()
    wire = encode(KIND_UUID, sid.bytes)
    frame = next(iter(FrameParser().feed(wire)))
    check(frame.is_uuid, "frame reports as uuid")
    check(frame.uuid_str() == str(sid), "uuid renders back to the canonical string")


def test_malformed_uuid_is_rejected_not_guessed():
    frame = next(iter(FrameParser().feed(encode(KIND_UUID, b"tooshort"))))
    check(frame.uuid_str() is None, "a wrong-length uuid payload yields None, not a guess")


def test_unknown_type_is_tolerated():
    """Asterisk versions differ on some type bytes; an unknown frame must not break the stream."""
    blob = encode(0x7E, b"???") + encode_audio(b"\x00\x01" * 160)
    frames = list(FrameParser().feed(blob))
    check(len(frames) == 2, "unknown frame is parsed, not fatal")
    check(not frames[0].is_known, "unknown type flagged as such")
    check(frames[1].is_audio, "the stream stays aligned after an unknown frame")


def test_zero_length_frame():
    frames = list(FrameParser().feed(encode(KIND_TERMINATE)))
    check(len(frames) == 1 and frames[0].is_terminate, "zero-length terminate parses")


def test_oversize_payload_raises():
    try:
        encode(KIND_AUDIO, b"\x00" * 70000)
    except ValueError:
        check(True, "oversize payload raises instead of truncating")
    else:
        check(False, "oversize payload raises instead of truncating")


def test_peak_detection():
    from app.session import peak_of

    silence = b"\x00\x00" * 160
    check(peak_of(silence, 0) == 0, "digital silence reads as peak 0")
    loud = (12345).to_bytes(2, "little", signed=True) * 160
    check(peak_of(loud, 0) == 12345, "positive peak detected")
    neg = (-4242).to_bytes(2, "little", signed=True) * 160
    check(peak_of(neg, 0) == 4242, "negative samples counted by absolute value")
    check(peak_of(silence, 999) == 999, "running peak is never lowered")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"\n{t.__name__}")
        t()
    print(f"\n{_checks} checks passed across {len(tests)} tests.")

"""Unit test for the pure dual-channel merge logic (audio.merge_channels).

No ffmpeg, no API, no DB — just fabricated per-channel segments through the interleave
function. ffmpeg splitting + Whisper are validated once manually on a real stereo call.

Run: python -m tests.test_merge_channels
"""

import sys

from app.analysis.audio import merge_channels


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"merge_channels failed at: {name}")


def main():
    print("merge_channels — interleave + speaker labeling:")

    caller = [
        {"start": 0.0, "end": 2.0, "text": "Hi, I saw your ad for the truck."},
        {"start": 5.0, "end": 6.5, "text": "Is it still available?"},
    ]
    operator = [
        {"start": 2.1, "end": 4.9, "text": "Yes it is, thanks for calling."},
        {"start": 6.6, "end": 8.0, "text": "It sure is."},
    ]

    text, segments = merge_channels(caller, operator)

    # Chronological interleave by start time.
    order = [(s["speaker"], s["text"]) for s in segments]
    check("segments sorted chronologically across channels", order == [
        ("caller", "Hi, I saw your ad for the truck."),
        ("operator", "Yes it is, thanks for calling."),
        ("caller", "Is it still available?"),
        ("operator", "It sure is."),
    ])

    # Flat text carries [Caller]/[Operator] line prefixes in the same order.
    check("flat text is speaker-labeled in order", text.splitlines() == [
        "[Caller] Hi, I saw your ad for the truck.",
        "[Operator] Yes it is, thanks for calling.",
        "[Caller] Is it still available?",
        "[Operator] It sure is.",
    ])

    # Every segment carries the structured shape.
    check("segment shape is {speaker,start,end,text}",
          all(set(s) == {"speaker", "start", "end", "text"} for s in segments))
    check("speaker is semantic role, not channel index",
          {s["speaker"] for s in segments} == {"caller", "operator"})

    # Empty / whitespace-only segments are dropped, not rendered as blank lines.
    text2, segs2 = merge_channels(
        [{"start": 0.0, "end": 1.0, "text": "  "}, {"start": 1.0, "end": 2.0, "text": "real"}],
        [],
    )
    check("blank segments dropped", [s["text"] for s in segs2] == ["real"])
    check("blank segments absent from flat text", text2 == "[Caller] real")

    # One-sided call (only operator spoke) still produces a valid transcript.
    text3, segs3 = merge_channels([], [{"start": 0.0, "end": 1.0, "text": "hello?"}])
    check("one-sided call handled", text3 == "[Operator] hello?" and len(segs3) == 1)

    # Missing start times default to 0.0 without crashing.
    text4, segs4 = merge_channels([{"text": "no timestamps"}], [])
    check("missing timestamps default to 0.0",
          segs4[0]["start"] == 0.0 and segs4[0]["end"] == 0.0)

    print("\nALL MERGE_CHANNELS CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)

"""A recorded forward must actually BRIDGE (regression for the 2026-08-05 live failure).

THE BUG: `record` on a dial node started a CHANNEL recording on the caller's leg before
originating. Asterisk then refuses to move that channel into a bridge:

    POST /ari/bridges/<id>/addChannel -> 409 {"message":"Channel 1785953643.61 currently recording"}

`add_to_bridge` ignored the result and `dial_number` returned "answered" regardless, so the
flow reported a connected call while both parties sat on dead air. Live call 1785953643.61:
25s, no bridge, and a caller-ONLY recording transcribing to 5 words.

WHY THE FIRST TEST MISSED IT: the fake ARI client accepted every op unconditionally, so it
was strictly more permissive than the real thing. The fake here ENFORCES the two real
constraints that matter:
  1. a channel with an active channel-recording cannot be added to a bridge (409);
  2. addChannel is all-or-nothing — a rejection joins NEITHER leg.
Any future change that reintroduces a pre-bridge channel recording fails here.

Covers the pure interpreter (does it ask for channel-record or bridge-record?) and the
client's contract (does a rejected bridge surface as `failed`?). Stdlib only.
Run: python -m tests.test_dial_record_bridge
"""

import asyncio

from app.flows.interpreter import FlowInterpreter

LINKEDID = "1785953643.61"
CHAN = "1785953643.61"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"dial_record_bridge failed at: {name}")


class AriRejectsBridgeWhileRecording:
    """Fake ARI that models the REAL constraints — see module docstring.

    `dial_number` here mirrors the client's own sequence (originate -> bridge -> record) so
    the 409 is reproduced at the same seam the live failure occurred at."""

    def __init__(self, dial_result="answered"):
        self.ops = []
        self.recording_channels = set()   # channels with an active CHANNEL recording
        self.bridged = []                 # channels successfully joined to a bridge
        self.bridge_recordings = []       # names of BRIDGE recordings started
        self._dial_result = dial_result

    async def answer(self, channel_id):
        self.ops.append(("answer", channel_id))

    async def play(self, channel_id, media):
        self.ops.append(("play", channel_id))

    async def play_and_wait(self, channel_id, media, *, timeout_s=30.0):
        self.ops.append(("play_and_wait", channel_id))

    async def record(self, channel_id, name):
        """Channel recording — the op that poisons a later bridge."""
        self.ops.append(("record_channel", channel_id))
        self.recording_channels.add(channel_id)

    async def read_digit(self, channel_id, *, prompt, timeout_s, max_digits):
        return "1"

    async def _add_to_bridge(self, *channel_ids) -> bool:
        """All-or-nothing, and refuses any channel that is currently recording (ARI 409)."""
        if any(c in self.recording_channels for c in channel_ids):
            self.ops.append(("addChannel_409", channel_ids))
            return False
        self.ops.append(("addChannel_ok", channel_ids))
        self.bridged.extend(channel_ids)
        return True

    async def dial_number(self, channel_id, number, *, caller_id, timeout_s, record_name=None):
        self.ops.append(("dial", number))
        if self._dial_result != "answered":
            return self._dial_result
        out_id = "flow-dial-fake"
        if not await self._add_to_bridge(channel_id, out_id):
            return "failed"          # the client's contract: no bridge => not answered
        if record_name:
            self.ops.append(("record_bridge", record_name))
            self.bridge_recordings.append(record_name)
        return "answered"

    async def dial_operator(self, channel_id, operators, *, caller_id, timeout_s, record_name=None):
        return await self.dial_number(
            channel_id, "operator", caller_id=caller_id, timeout_s=timeout_s,
            record_name=record_name,
        )

    async def voicemail(self, channel_id, *, greeting, name, max_duration_s, max_silence_s):
        self.ops.append(("voicemail", channel_id))

    async def hangup(self, channel_id):
        self.ops.append(("hangup", channel_id))

    def kinds(self):
        return [o[0] for o in self.ops]


async def _noop_emit(event_type, seq, payload):
    return None


def _graph(dial_extra=None, play_record=False):
    """The live flow's shape: entry -> play(consent) -> dial(+18583794393)."""
    return {
        "default_fallback": "vm",
        "nodes": {
            "entry": {"type": "entry", "next": {"default": "play"}},
            "play": dict(
                {"type": "play", "prompt": "This call might be recorded"},
                **({"record": True} if play_record else {}),
                next={"default": "dial"},
            ),
            "dial": dict(
                {"type": "dial", "target": "+18583794393"},
                **(dial_extra or {}),
                next={"answered": "bye", "failed": "vm", "busy": "vm", "noanswer": "vm"},
            ),
            "vm": {"type": "voicemail"},
            "bye": {"type": "hangup"},
        },
    }


def _run(ari, graph):
    asyncio.run(
        FlowInterpreter(
            graph=graph, channel_id=CHAN, ari=ari, emit=_noop_emit, linkedid=LINKEDID
        ).run()
    )
    return ari


def test_recorded_dial_still_bridges():
    """THE regression: record:true must NOT stop the call connecting."""
    print("a dial node with record:true still bridges:")
    ari = _run(AriRejectsBridgeWhileRecording(), _graph({"record": True}))

    check("no channel recording was started", "record_channel" not in ari.kinds())
    check("bridge was accepted (no 409)", "addChannel_409" not in ari.kinds())
    check("both legs joined", len(ari.bridged) == 2)
    check("the BRIDGE was recorded", len(ari.bridge_recordings) == 1)
    check("recording named for the pipeline", ari.bridge_recordings[0].startswith(LINKEDID))
    check("flow took the answered port", "hangup" in ari.kinds() and "voicemail" not in ari.kinds())


def test_unrecorded_dial_bridges_without_recording():
    print("a dial node without record still bridges, and records nothing:")
    ari = _run(AriRejectsBridgeWhileRecording(), _graph())
    check("both legs joined", len(ari.bridged) == 2)
    check("no bridge recording", ari.bridge_recordings == [])
    check("no channel recording", "record_channel" not in ari.kinds())


def test_rejected_bridge_takes_the_failed_port():
    """If a bridge IS rejected, the caller must not be left on dead air believing it worked.
    Simulated by a play node that starts a channel recording before the dial — the exact
    shape that poisoned the live call."""
    print("a rejected bridge surfaces as `failed`, not a silent dead-air 'answered':")
    ari = _run(AriRejectsBridgeWhileRecording(), _graph(play_record=True))

    check("the 409 happened (fake enforced it)", "addChannel_409" in ari.kinds())
    check("no legs were joined", ari.bridged == [])
    check("flow fell to voicemail, not the answered port", "voicemail" in ari.kinds())


def test_operator_dial_passes_record_through():
    print("an operator-target dial records the bridge too:")
    ari = _run(
        AriRejectsBridgeWhileRecording(),
        _graph({"record": True, "target_kind": "operator", "operator": "jane@x.com"}),
    )
    check("no channel recording", "record_channel" not in ari.kinds())
    check("bridge recorded", len(ari.bridge_recordings) == 1)


if __name__ == "__main__":
    test_recorded_dial_still_bridges()
    test_unrecorded_dial_bridges_without_recording()
    test_rejected_bridge_takes_the_failed_port()
    test_operator_dial_passes_record_through()
    print("\nALL DIAL RECORD/BRIDGE CHECKS PASSED")

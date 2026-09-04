"""Call-ownership + take-over tests (AI_AGENT_SPEC D4/D5).

The invariant under test is the one that keeps a supervisor's rescue from being undone by the
automation they rescued the caller from:

    Once a call is human-owned, no automated path may hang up, play, record or bridge it.

Pure: no ARI, no DB, no event loop beyond a trivial runner.

Run:  python -m tests.test_ownership      (from backend/)
"""

import asyncio
import sys

sys.path.insert(0, ".")

from app.flows.interpreter import PORT_TAKEN_OVER, FlowInterpreter  # noqa: E402
from app.telephony import ownership  # noqa: E402

_checks = 0


def check(cond, label):
    global _checks
    _checks += 1
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    print(f"  ok  {label}")


def test_claim_and_release():
    ownership.clear()
    check(not ownership.is_owned("call-1"), "a call starts unowned")
    check(ownership.claim("call-1", "sam@x.com", channels=["ch-a", "ch-b"]), "claim succeeds")
    check(ownership.is_owned("call-1"), "call is owned after claiming")
    check(ownership.owner_of("call-1") == "sam@x.com", "owner is recorded")
    check(ownership.is_channel_owned("ch-a"), "channel a is owned")
    check(ownership.is_channel_owned("ch-b"), "channel b is owned")
    check(not ownership.is_channel_owned("ch-other"), "an unrelated channel is not owned")
    ownership.release("call-1")
    check(not ownership.is_owned("call-1"), "released")
    check(not ownership.is_channel_owned("ch-a"), "channels released with the call")


def test_second_supervisor_is_refused():
    """Two people seizing one call is a race that must be lost loudly."""
    ownership.clear()
    ownership.claim("call-2", "first@x.com", channels=["ch"])
    check(not ownership.claim("call-2", "second@x.com"), "a different owner is refused")
    check(ownership.owner_of("call-2") == "first@x.com", "the first owner keeps the call")
    check(ownership.claim("call-2", "first@x.com"), "the SAME owner may re-claim (idempotent)")


def test_channel_added_after_claim():
    ownership.clear()
    ownership.claim("call-3", "sam@x.com", channels=["caller"])
    ownership.add_channel("call-3", "operator-leg")
    check(ownership.is_channel_owned("operator-leg"), "a later channel joins the ownership")
    check(ownership.linkedid_of_channel("operator-leg") == "call-3", "reverse lookup works")


# --- the interpreter must stand down ----------------------------------------------------

class _FakeAri:
    """Records every operation, so 'touched nothing' is provable rather than asserted."""

    def __init__(self):
        self.calls = []

    async def answer(self, channel_id):
        self.calls.append(("answer", channel_id))

    async def play(self, channel_id, media):
        self.calls.append(("play", channel_id))

    async def hangup(self, channel_id):
        self.calls.append(("hangup", channel_id))

    async def record(self, channel_id, name):
        self.calls.append(("record", channel_id))

    async def read_digit(self, channel_id, *, prompt, timeout_s, max_digits):
        return None

    async def dial_number(self, channel_id, number, *, caller_id, timeout_s,
                          record_name=None):
        self.calls.append(("dial", number))
        return "answered"

    async def voicemail(self, channel_id, *, greeting, name, max_duration_s, max_silence_s):
        self.calls.append(("voicemail", channel_id))


def _run(graph, run_agent):
    ari = _FakeAri()
    events = []

    async def emit(t, seq, payload):
        events.append((t, payload))

    interp = FlowInterpreter(
        graph=graph, channel_id="ch-1", ari=ari, emit=emit, linkedid="call-x",
        run_agent=run_agent,
    )
    asyncio.run(interp.run())
    return ari, events


GRAPH = {
    "default_fallback": "vm",
    "nodes": {
        "start": {"type": "entry", "next": {"default": "agent"}},
        # NOTE: no edge for the agent's exit — exactly the shape that used to fall through
        # to default_fallback and play voicemail over a human conversation.
        "agent": {"type": "ai_agent", "agent_id": "a1", "next": {}},
        "vm": {"type": "voicemail", "greeting": "leave a message"},
    },
}


def test_taken_over_stands_down():
    async def run_agent(node):
        return (PORT_TAKEN_OVER, {})

    ari, events = _run(GRAPH, run_agent)
    ops = [c[0] for c in ari.calls]
    check("voicemail" not in ops, "NO voicemail played over the human's conversation")
    check(ops.count("hangup") == 0, "the caller is NOT hung up on")
    check(ops == ["answer"], f"only the entry answer happened, got {ops}")
    summary = [p for t, p in events if t == "flow.call.summary"]
    check(summary and summary[0]["flow"]["ended"] == "taken_over",
          "the call summary records 'taken_over'")


def test_without_takeover_the_old_behaviour_still_applies():
    """Control: an ordinary agent exit with no wired edge still falls back, as designed."""
    async def run_agent(node):
        return ("default", {})

    ari, _ = _run(GRAPH, run_agent)
    ops = [c[0] for c in ari.calls]
    check("voicemail" in ops, "a NORMAL unwired exit still routes to the fallback")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"\n{t.__name__}")
        t()
    ownership.clear()
    print(f"\n{_checks} checks passed across {len(tests)} tests.")

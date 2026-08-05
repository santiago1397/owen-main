"""`play` node blocks until its prompt finishes (regression for the 2026-08-05 live failure).

THE BUG: `_h_play` used the fire-and-forget `play()`, which returns as soon as ARI ACCEPTS
the playback. On the live "ucallz->18583794393" flow (entry -> menu -> play -> dial) that
meant the recording-consent notice was still mid-sentence when the interpreter originated
and bridged the outbound leg — `flow.node.play` and `flow.node.dial` were emitted 13ms
apart on call 1785950003.49. FL is all-party consent (ARCHITECTURE.md #17), so a notice the
caller never hears is worse than none.

This is the SAME class of bug already fixed for the menu prompt (test_menu_prompt_timing.py):
ARI accepting a playback is not the playback finishing.

Asserts:
- the `dial` that follows a consent `play` starts only AFTER the prompt finishes;
- a client without `play_and_wait` still plays (degrades to fire-and-forget, never dead air);
- a node carrying only `label` (the canvas's operator-facing title) plays NOTHING — the
  live misconfiguration that hid the bug, kept as an executable warning;
- `record` on a play node still fires before the prompt.

Pure interpreter + fakes, stdlib only. Run: python -m tests.test_play_node_consent
"""

import asyncio
import time

from app.flows.interpreter import FlowInterpreter

LINKEDID = "1785950003.49"
CHAN = "1785950003.49"

CONSENT = "This call might be recorded"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"play_node_consent failed at: {name}")


class BlockingAri:
    """Fake with the FULL surface: `play_and_wait` takes `prompt_s` to finish, like a real
    prompt. Records (op, elapsed) so ORDERING IN TIME — not just call order — is assertable."""

    def __init__(self, prompt_s=0.4, dial_result="answered"):
        self.calls = []          # (op, arg, elapsed_seconds)
        self._prompt_s = prompt_s
        self._dial_result = dial_result
        self._t0 = None

    def _mark(self, op, arg=None):
        if self._t0 is None:
            self._t0 = time.monotonic()
        self.calls.append((op, arg, time.monotonic() - self._t0))

    async def answer(self, channel_id):
        self._mark("answer")

    async def play(self, channel_id, media):
        self._mark("play", media)

    async def play_and_wait(self, channel_id, media, *, timeout_s=30.0):
        self._mark("play_and_wait", media)
        await asyncio.sleep(self._prompt_s)          # the prompt actually takes time
        self._mark("play_finished", media)

    async def record(self, channel_id, name):
        self._mark("record", name)

    async def read_digit(self, channel_id, *, prompt, timeout_s, max_digits):
        self._mark("read_digit", prompt)
        return "1"

    async def dial_number(self, channel_id, number, *, caller_id, timeout_s):
        self._mark("dial", number)
        return self._dial_result

    async def dial_operator(self, channel_id, operators, *, caller_id, timeout_s):
        self._mark("dial_operator", tuple(operators))
        return self._dial_result

    async def voicemail(self, channel_id, *, greeting, name, max_duration_s, max_silence_s):
        self._mark("voicemail", greeting)

    async def hangup(self, channel_id):
        self._mark("hangup")

    def ops(self):
        return [c[0] for c in self.calls]

    def at(self, op):
        """Elapsed seconds at which `op` was recorded (first occurrence)."""
        return next(c[2] for c in self.calls if c[0] == op)


class LegacyAri(BlockingAri):
    """A client implementing only the minimum AriControl surface — no `play_and_wait`."""

    play_and_wait = None


async def _noop_emit(event_type, seq, payload):
    return None


def _graph(play_node):
    """The live flow's shape, reduced to the path under test: entry -> play -> dial."""
    return {
        "default_fallback": "vm",
        "nodes": {
            "entry": {"type": "entry", "next": {"default": "play"}},
            "play": dict(play_node, next={"default": "dial"}),
            "dial": {"type": "dial", "target": "+18583794393", "next": {}},
            "vm": {"type": "voicemail"},
        },
    }


def _run(ari, play_node):
    interp = FlowInterpreter(
        graph=_graph(play_node),
        channel_id=CHAN,
        ari=ari,
        emit=_noop_emit,
        linkedid=LINKEDID,
    )
    asyncio.run(interp.run())
    return ari


def test_dial_waits_for_consent_prompt():
    """THE regression. The prompt takes 0.4s; the dial must not start before it ends."""
    print("dial starts only after the consent prompt finishes:")
    ari = _run(BlockingAri(prompt_s=0.4), {"type": "play", "prompt": CONSENT})

    check("prompt was played", "play_and_wait" in ari.ops())
    check("dial happened", "dial" in ari.ops())
    check(
        "dial ordered after the prompt FINISHED",
        ari.ops().index("play_finished") < ari.ops().index("dial"),
    )
    # The ordering check above would pass even on the old code if the fake were instant, so
    # assert the real thing: wall-clock separation. Old behaviour was ~0ms (live: 13ms).
    check("dial deferred by the prompt's duration", ari.at("dial") - ari.at("play_and_wait") >= 0.35)


def test_client_without_play_and_wait_still_plays():
    """A minimal client must degrade to fire-and-forget, never skip the prompt or crash."""
    print("client lacking play_and_wait degrades to fire-and-forget:")
    ari = _run(LegacyAri(), {"type": "play", "prompt": CONSENT})
    check("fell back to plain play", "play" in ari.ops())
    check("prompt text still passed through", (("play", CONSENT) in [(c[0], c[1]) for c in ari.calls]))
    check("flow still reached dial", "dial" in ari.ops())


def test_label_only_node_plays_nothing():
    """The live misconfiguration: the consent text was typed into the canvas's 'Label'
    (operator-facing title) instead of 'Prompt'. `_media` reads media/prompt only, so NOTHING
    is played. Pinned as a test so the trap is visible in code, not just in a post-mortem."""
    print("a label-only play node plays nothing (the live misconfiguration):")
    ari = _run(BlockingAri(), {"type": "play", "label": CONSENT})
    check("no playback attempted", "play_and_wait" not in ari.ops() and "play" not in ari.ops())
    check("flow still advanced to dial", "dial" in ari.ops())


def test_record_modifier_fires_before_prompt():
    print("record modifier still starts before the prompt:")
    ari = _run(BlockingAri(), {"type": "play", "prompt": CONSENT, "record": True})
    check("record started", "record" in ari.ops())
    check("record ordered before the prompt", ari.ops().index("record") < ari.ops().index("play_and_wait"))


if __name__ == "__main__":
    test_dial_waits_for_consent_prompt()
    test_client_without_play_and_wait_still_plays()
    test_label_only_node_plays_nothing()
    test_record_modifier_fires_before_prompt()
    print("\nALL PLAY NODE CONSENT CHECKS PASSED")

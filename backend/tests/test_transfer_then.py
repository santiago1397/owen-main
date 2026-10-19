"""A transfer rings its follow-up numbers in order when nobody answers (2026-10-19, owner):
"12 s to the main line, then 15 s to another number".

Run: python -m tests.test_transfer_then
"""
import asyncio
import sys

from app.agents.service import validate_agent_config
from app.flows import runtime
from app.flows.transfer import resolve_transfer_target

FAILS = []


def check(name, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + name)
    if not cond:
        FAILS.append(name)


MAIN, SECOND = "+19549147244", "+15615550100"
TARGETS = {"office": {"kind": "number", "target": MAIN, "ring_seconds": 12,
                      "then": [{"kind": "number", "target": SECOND, "ring_seconds": 15}]}}


class Ari:
    def __init__(self, outcomes, caller_left_on=None):
        self.outcomes, self.dialed, self.hung = list(outcomes), [], []
        self.caller_left_on = caller_left_on
        self._last_dial = {}

    async def dial_number(self, channel_id, target, *, caller_id, timeout_s):
        self.dialed.append((target, timeout_s))
        out = self.outcomes.pop(0)
        self._last_dial = {"dial_ended_by": "caller"} if target == self.caller_left_on else {}
        return out

    async def hangup(self, channel_id):
        self.hung.append(channel_id)


def run(outcomes, caller_left_on=None):
    ari = Ari(outcomes, caller_left_on)
    chosen = resolve_transfer_target(TARGETS, "office")
    res = asyncio.run(runtime._agent_transfer_outcome(ari, "caller-1", "lid-1", chosen))
    return res, ari


chosen = resolve_transfer_target(TARGETS, "office")
check("the follow-up step is carried with its own ring time",
      chosen["then"] == [{"kind": "number", "target": SECOND, "ring_seconds": 15}])

res, ari = run(["noanswer", "answered"])
check("main line unanswered for 12 s -> second number rings for 15 s -> answered",
      res == "answered" and ari.dialed == [(MAIN, 12.0), (SECOND, 15.0)] and ari.hung == ["caller-1"])

res, ari = run(["answered"])
check("main line answers -> the second number never rings", res == "answered" and ari.dialed == [(MAIN, 12.0)])

res, ari = run(["noanswer", "noanswer"])
check("both unanswered -> not handled (the caller goes back to the agent)",
      res == "noanswer" and len(ari.dialed) == 2)

res, ari = run(["busy", "answered"])
check("main line busy -> the second number rings", res == "answered" and len(ari.dialed) == 2)

res, ari = run(["noanswer", "answered"], caller_left_on=MAIN)
check("the caller hung up while the main line rang -> nothing else rings",
      res == "caller_gone" and ari.dialed == [(MAIN, 12.0)])

base = {"engine": "retell", "retell_agent_id": "agent_x", "tools": {"transfer": True}}
def errs(t):
    errors, _warnings = validate_agent_config({**base, "transfer_targets": t})
    return errors
check("a valid follow-up passes validation", not errs(TARGETS))
check("a follow-up with no target is refused",
      any("no target" in e for e in errs({"office": {**TARGETS["office"], "then": [{"kind": "number"}]}})))
check("more than two follow-ups is refused",
      any("at most" in e for e in errs({"office": {**TARGETS["office"], "then": [TARGETS["office"]["then"][0]] * 3}})))
check("a bad follow-up ring time is refused",
      any("ring_seconds" in e for e in errs({"office": {**TARGETS["office"],
                                              "then": [{"kind": "number", "target": SECOND, "ring_seconds": 200}]}})))

if FAILS:
    print(f"\n{len(FAILS)} FAILED"); sys.exit(1)
print("\nALL TRANSFER-THEN CHECKS PASSED")

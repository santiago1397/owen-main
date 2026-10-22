"""An office line that forwards the caller back to us counts as "nobody answered", and the caller
waits to music (2026-10-22, owner).

Quo and Zuper Connect pick a transferred call up themselves and, when nobody answers, forward it to
an AI number - ours - as a NEW inbound call with the caller's own number. Before this the caller met
the agent again, greeted from scratch (live, 2026-10-22 4:45 PM).

Run: python -m tests.test_transfer_loop_guard
"""
import asyncio
import sys

from app.flows import runtime
from app.flows.transfer import resolve_transfer_target

# Transfers ring only Mon-Sat 8-5 (2026-10-22): these checks run on a Thursday morning.
from datetime import datetime as _dt  # noqa: E402
from zoneinfo import ZoneInfo as _Z  # noqa: E402
runtime._office_now = lambda: _dt(2026, 10, 22, 10, 0, tzinfo=_Z("America/New_York"))


FAILS = []


def check(name, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + name)
    if not cond:
        FAILS.append(name)


QUO, MOBILE, CALLER = "+19549147244", "+18583794393", "+18632244548"
TARGETS = {"office": {"kind": "number", "target": QUO, "ring_seconds": 12,
                      "then": [{"kind": "number", "target": MOBILE, "ring_seconds": 15}]}}


class Ari:
    """A dialler whose first number "answers" (a platform answer) and then bounces the caller
    back to us while the leg is up; the second number answers for real or not."""

    def __init__(self, second="answered", bounce=True):
        self.second, self.bounce = second, bounce
        self.dialed, self.hung, self.kwargs = [], [], []
        self._last_dial = {}

    async def dial_number(self, channel_id, target, *, caller_id, timeout_s, hold_music=False):
        self.dialed.append(target)
        self.kwargs.append(hold_music)
        self._last_dial = {"dial_out_leg": "out-%d" % len(self.dialed)}
        if target == QUO:
            if self.bounce:
                # Quo forwards the caller to our AI number: a new inbound call arrives.
                handled = await runtime.bounced_transfer(self, "bounce-ch", "lid-2", CALLER)
                self.bounced_handled = handled
            return "answered"
        return self.second

    async def hangup(self, channel_id):
        self.hung.append(channel_id)


def run(ari, caller=CALLER):
    chosen = resolve_transfer_target(TARGETS, "office")
    return asyncio.run(runtime._agent_transfer_outcome(ari, "caller-1", "lid-1", chosen,
                                                       caller_number=caller))


ari = Ari()
res = run(ari)
check("Quo forwards the caller back -> the bounce is caught and both legs hung up",
      ari.bounced_handled and "out-1" in ari.hung and "bounce-ch" in ari.hung)
check("...it counts as unanswered, so the mobile rings next and answers",
      res == "answered" and ari.dialed == [QUO, MOBILE])
check("the caller waits to music on every number", ari.kwargs == [True, True])
check("nothing is left registered after the transfer", runtime.ACTIVE_TRANSFERS == {})

ari = Ari(second="noanswer")
res = run(ari)
check("bounced, then the mobile unanswered -> not handled (the agent takes the caller back "
      "with the sorry message)", res == "noanswer" and "caller-1" not in ari.hung)

ari = Ari(bounce=False)
res = run(ari)
check("a real answer on the office line stays answered (the mobile never rings)",
      res == "answered" and ari.dialed == [QUO])


async def other_caller():
    return await runtime.bounced_transfer(Ari(), "ch-x", "lid-9", "+15615550000")


check("an unrelated new call is never touched", asyncio.run(other_caller()) is False)


class OldAri:
    """A dialler without hold music (the test doubles elsewhere): still works."""
    _last_dial = {}

    async def dial_number(self, channel_id, target, *, caller_id, timeout_s):
        return "answered"

    async def hangup(self, channel_id):
        pass


check("a dialler without hold music is called the old way",
      run(OldAri()) == "answered")

# ---- office hours (2026-10-22, owner): transfers only Mon-Sat 8 AM - 5 PM New York
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from app.flows.transfer import office_open  # noqa: E402
from app.integrations.retell import brief  # noqa: E402

NY = ZoneInfo("America/New_York")
check("open Mon-Sat 8-5, closed at night and on Sunday",
      office_open(datetime(2026, 10, 22, 9, 0, tzinfo=NY))
      and not office_open(datetime(2026, 10, 22, 20, 51, tzinfo=NY))
      and not office_open(datetime(2026, 10, 25, 11, 0, tzinfo=NY))
      and office_open(datetime(2026, 10, 24, 16, 59, tzinfo=NY)))
saved = runtime._office_now
runtime._office_now = lambda: datetime(2026, 10, 22, 20, 51, tzinfo=NY)
try:
    ari = Ari()
    res = run(ari)
    check("at 8:51 PM a transfer never dials -> closed", res == "closed" and ari.dialed == [])
finally:
    runtime._office_now = saved  # back to Thursday morning
back = brief.after_failed_transfer({"greeting": "Hello?"}, closed=True)
check("...and Anisa says the office is closed",
      back["greeting"] == brief.GREETING_OFFICE_CLOSED and "closed" in back["greeting"])

print("\n%d failed" % len(FAILS) if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)

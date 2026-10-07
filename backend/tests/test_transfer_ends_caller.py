"""An answered agent transfer that ends hangs up the caller's leg too (2026-10-06).

`dial_number` / `dial_operator` return "answered" once EITHER leg leaves; the interpreter then
stands down. Without this the caller sat in silence when the office hung up first.

Run: python -m tests.test_transfer_ends_caller
"""
import asyncio
import sys

from app.flows import runtime

FAILS = []


def check(name, cond):
    print(("  [PASS] " if cond else "  [FAIL] ") + name)
    if not cond:
        FAILS.append(name)


class Ari:
    def __init__(self, outcome):
        self.outcome, self.hung = outcome, []

    async def dial_number(self, channel_id, target, **kw):
        return self.outcome

    async def dial_operator(self, channel_id, targets, **kw):
        return self.outcome

    async def hangup(self, channel_id):
        self.hung.append(channel_id)


def run(kind, outcome):
    ari = Ari(outcome)
    ok = asyncio.run(runtime._do_agent_transfer(
        ari, "caller-1", "lid-1", {"kind": kind, "target": "+19549147244" if kind == "number"
                                   else "operator-x"}))
    return ok, ari.hung


for kind in ("number", "operator"):
    ok, hung = run(kind, "answered")
    check(f"{kind}: answered transfer reports handled", ok is True)
    check(f"{kind}: the caller's leg is hung up when the conversation ends", hung == ["caller-1"])
    for outcome in ("noanswer", "busy", "failed"):
        ok, hung = run(kind, outcome)
        check(f"{kind}: {outcome} is not handled and the caller is NOT hung up "
              "(the flow's fallback takes them)", ok is False and hung == [])

if FAILS:
    print(f"\n{len(FAILS)} FAILED")
    sys.exit(1)
print("\nALL TRANSFER-END CHECKS PASSED")

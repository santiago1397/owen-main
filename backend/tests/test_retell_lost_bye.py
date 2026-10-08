"""A lost SIP BYE can never leave a caller in dead air (2026-10-06).

On the first agent-ended production call Retell's BYE never reached Asterisk (a firewall),
so ARI never said the Retell leg was gone and the caller sat in silence until they hung up.
The safety net: Retell's `call_ended` webhook, arriving while the call is still live here,
is seen by the engine's wait loop (`registry.retell_said_ended`), which then ends the agent's
part exactly as a real Retell hang-up does.

Driven end to end: the REAL engine (`agents/retell.py`) against a fake ARI that never reports
the Retell leg gone, and the REAL webhook handler (`integrations/retell/webhook.handle`)
posting into the same in-memory registry, with the transcript store and the CRM hop recorded.

  1. call_ended on a live call -> end_call, the Retell leg hung up FROM HERE, the bridge
     destroyed, the caller never touched (the flow's `end` node does that).
  2. A duplicate delivery is harmless; a delivery whose processing failed (claim released for
     Retell's retry) still ends the call.
  3. call_ended after the leg is already gone -> no ARI operation at all.
  4. A take-over is not overridden: taken_over, nothing hung up or destroyed.
  5. An earlier transfer request wins: the transfer port, with its destination.
  6. call_analyzed ends nothing.

Run: python -m tests.test_retell_lost_bye
"""

import asyncio

from tests import retell_guard
from tests.retell_support import (CHANNEL, LINKEDID, FakeAri, MemoryRegistry, MockRetell,
                                  Patch, leg_gone)
from tests.retell_support import check as _check
from tests.test_retell_engine import _run, _spec


def check(name, cond):
    _check(name, cond, "retell_lost_bye")


CALL_ID = "call_abc123"
ENDED = {"event": "call_ended", "call": {"call_id": CALL_ID, "agent_version": 6,
                                         "disconnection_reason": "agent_hangup",
                                         "duration_ms": 61000}}
ANALYZED = {"event": "call_analyzed", "call": {
    "call_id": CALL_ID, "call_analysis": {"call_summary": "Leak reported."}}}


class Hooks:
    """The webhook's database and CRM steps, recorded (so "nothing done" is a count)."""

    def __init__(self):
        self.persisted, self.reported = [], []
        self.fail_persist = False

    def __enter__(self):
        from app.agents import spend
        from app.integrations.retell import webhook

        async def persist(snap, call):
            if self.fail_persist:
                raise RuntimeError("database down")
            self.persisted.append(snap["retell_call_id"])

        async def report(kind, snap, call, segments=None):
            self.reported.append(kind)
            return True

        async def no_alert():
            return None

        self.patch = Patch((webhook, "persist_ended", persist), (webhook, "report", report),
                           (spend, "check_alert", no_alert))
        self.patch.__enter__()
        return self

    def __exit__(self, *exc):
        self.patch.__exit__(*exc)
        return False

    async def post(self, reg, body):
        from app.integrations.retell import webhook

        return await webhook.handle(body, reg)


def test_call_ended_on_a_live_call_ends_the_retell_leg_from_here():
    print("call_ended arrives, the BYE never does:")
    reg, answers = MemoryRegistry(), []

    with Hooks() as hooks:
        async def script(ari):
            await asyncio.sleep(0.03)
            answers.append(await hooks.post(reg, ENDED))
            answers.append(await hooks.post(reg, ENDED))      # Retell's retry

        ari = FakeAri(script=script)
        result = _run(_spec(), ari, MockRetell(), reg)

        check("the webhook was processed once, the retry was a duplicate",
              answers == [(200, {"ok": True}), (200, {"duplicate": True})]
              and hooks.persisted == [CALL_ID] and hooks.reported == ["ended"])
        check("end_call, as if Retell had hung up", result.port == "end_call"
              and result.data.get("ended_by") == "retell")
        check("the Retell leg is hung up FROM HERE (no BYE ever came)",
              ("hangup", ari.out_id) in ari.ops)
        check("exactly once", ari.ops.count(("hangup", ari.out_id)) == 1)
        check("the bridge is destroyed", ("destroy_bridge", "bridge-1") in ari.ops)
        check("the CALLER is never hung up by the engine or the webhook (the flow decides)",
              ("hangup", CHANNEL) not in ari.ops)
        check("the row ends 'ended'", reg.rows[CALL_ID]["status"] == "ended")

        before = list(ari.ops)
        answer = asyncio.run(hooks.post(reg, ENDED))
        check("a third delivery after the call: a duplicate, no ARI operation",
              answer == (200, {"duplicate": True}) and ari.ops == before)


def test_a_released_claim_still_ends_the_call():
    print("call_ended whose processing failed (released for Retell's retry):")
    reg, answers = MemoryRegistry(), []

    with Hooks() as hooks:
        hooks.fail_persist = True

        async def script(ari):
            await asyncio.sleep(0.03)
            answers.append(await hooks.post(reg, ENDED))

        ari = FakeAri(script=script)
        result = _run(_spec(), ari, MockRetell(), reg)
        check("500 so Retell retries, and the claim was released",
              answers and answers[0][0] == 500 and reg.rows[CALL_ID]["ended_event_at"] is None)
        check("the call still ended (disconnection_reason survives the release)",
              result.port == "end_call" and ("hangup", ari.out_id) in ari.ops)


def test_call_ended_after_the_leg_is_gone_does_nothing():
    print("the BYE arrives normally; call_ended comes after:")
    reg = MemoryRegistry()

    async def script(ari):
        from app.flows import dtmf

        await asyncio.sleep(0.03)
        dtmf.push_channel_event(ari.out_id, leg_gone(ari.out_id))

    ari = FakeAri(script=script)
    result = _run(_spec(), ari, MockRetell(), reg)
    check("end_call from ARI", result.port == "end_call")
    check("the gone leg is not hung up again", ("hangup", ari.out_id) not in ari.ops)
    with Hooks() as hooks:
        before = list(ari.ops)
        answer = asyncio.run(hooks.post(reg, ENDED))
        check("the webhook is processed (transcript, cost, report)",
              answer == (200, {"ok": True}) and hooks.reported == ["ended"])
        check("...and no ARI operation at all", ari.ops == before)


def test_a_take_over_is_not_overridden():
    print("a supervisor took over, then call_ended:")
    from app.telephony import ownership

    reg = MemoryRegistry()
    with Hooks() as hooks:
        async def script(ari):
            await asyncio.sleep(0.03)
            ownership.claim(LINKEDID, "owen-desk", channels=[CHANNEL, "op-chan"])
            await hooks.post(reg, ENDED)

        ari = FakeAri(script=script)
        try:
            result = _run(_spec(), ari, MockRetell(), reg)
            check("taken_over", result.port == "taken_over")
            check("nothing hung up", "hangup" not in ari.names())
            check("the bridge the operator is joining is NOT destroyed",
                  "destroy_bridge" not in ari.names())
        finally:
            ownership.clear()


def test_an_earlier_transfer_request_wins():
    print("the agent asked to transfer, then Retell's call_ended came:")
    reg = MemoryRegistry()
    with Hooks() as hooks:
        async def script(ari):
            await asyncio.sleep(0.03)
            # Both land between two polls: the transfer was asked first, and wins.
            await reg.request_exit(CALL_ID, "transfer", {"destination": "office"})
            await hooks.post(reg, ENDED)

        ari = FakeAri(script=script)
        result = _run(_spec(), ari, MockRetell(), reg)
        check("the transfer port, with its destination",
              result.port == "transfer" and result.data.get("destination") == "office")
        check("the Retell leg is hung up so the caller can be moved",
              ("hangup", ari.out_id) in ari.ops)
        check("the caller is not hung up", ("hangup", CHANNEL) not in ari.ops)


def test_call_analyzed_ends_nothing():
    print("call_analyzed arrives mid-call:")
    reg = MemoryRegistry()
    seen = {}
    with Hooks() as hooks:
        async def script(ari):
            from app.flows import dtmf

            await asyncio.sleep(0.03)
            await hooks.post(reg, ANALYZED)
            await asyncio.sleep(0.15)                  # many polls (POLL_SECONDS = 0.01)
            seen["status"] = reg.rows[CALL_ID]["status"]
            seen["hangups"] = [o for o in ari.ops if o[0] == "hangup"]
            dtmf.push_channel_event(CHANNEL, leg_gone(CHANNEL))

        ari = FakeAri(script=script)
        result = _run(_spec(), ari, MockRetell(), reg)
        check("the analysis was reported", hooks.reported == ["analyzed"])
        check("the call was still live after it, nothing hung up",
              seen == {"status": "live", "hangups": []})
        check("it ended only when the caller hung up", result.port == "default")


def test_the_pure_predicate():
    print("registry.retell_said_ended:")
    from app.integrations.retell.registry import retell_said_ended

    check("nothing yet", not retell_said_ended({"ended_event_at": None}))
    check("no row", not retell_said_ended(None))
    check("claimed", retell_said_ended({"ended_event_at": "2026-10-06T12:00:00Z"}))
    check("released but its reason kept", retell_said_ended({"disconnection_reason": "x"}))
    check("an analysis alone is not an end",
          not retell_said_ended({"analyzed_event_at": "2026-10-06T12:00:00Z",
                                 "summary": "Leak reported."}))


if __name__ == "__main__":
    test_call_ended_on_a_live_call_ends_the_retell_leg_from_here()
    test_a_released_claim_still_ends_the_call()
    test_call_ended_after_the_leg_is_gone_does_nothing()
    test_a_take_over_is_not_overridden()
    test_an_earlier_transfer_request_wins()
    test_call_analyzed_ends_nothing()
    test_the_pure_predicate()
    retell_guard.assert_untouched()
    print("\nALL RETELL LOST-BYE CHECKS PASSED")

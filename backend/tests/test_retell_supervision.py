"""A live Retell call in the CRM's top bar, with Listen and Take over (RETELL-PLAN C7).

  1. **Listed** by `GET /api/crm-link/live-calls` in the SAME shape as an owen_voice call,
     plus `"engine": "retell"`; owen_voice rows keep their exact shape; no channel, bridge or
     Retell id leaves OWEN.
  2. **Listen** snoops the CALLER's channel; **Take over** queues the one `monitor_takeover`
     job OWEN already has, with the Retell SIP leg as the channel to eject and the call's
     bridge as the one the operator joins — taken from the registry, never the request.
  3. **The take-over actually ejects the agent.** `monitor.take_over` used to CLAIM the
     agent's channel before hanging it up, and a claimed channel is one the ARI client
     refuses to touch — so the hangup was a silent no-op. owen-voice hid it (it ends its own
     session); a Retell leg would have kept talking over the human.
  4. Nothing live -> 404, nothing queued.

Run: python -m tests.test_retell_supervision
"""

import asyncio
from datetime import datetime, timedelta, timezone

from tests import retell_guard
from tests.retell_support import CALLER, CHANNEL, DID, LINKEDID, MemoryRegistry, Patch, Settings
from tests.retell_support import check as _check

KNOWN = "owen@dreamteamroofingfl.com"
KNOWN_SLUG = "owen-dreamteamroofingfl.com"


def check(name, cond):
    _check(name, cond, "retell_supervision")


class FactsDb:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, stmt):
        rows = self.rows

        class R:
            def all(self_inner):
                return list(rows)
        return R()


class World:
    def __init__(self, voice_sessions=(), retell_live=True):
        self.reg = MemoryRegistry()
        if retell_live:
            self.reg.rows["call_abc123"] = {
                "retell_call_id": "call_abc123", "linkedid": LINKEDID, "status": "live",
                "call_channel_id": CHANNEL, "retell_channel_id": "flow-dial-retell-1",
                "bridge_id": "bridge-r1", "agent_name": "Receptionist",
                "created_at": datetime.now(timezone.utc) - timedelta(seconds=42)}
        self.voice = [dict(s) for s in voice_sessions]
        self.jobs = []

    def __enter__(self):
        from app.integrations.retell import registry
        from app.services import queue
        from app.telephony import voice_client

        async def active():
            return [dict(s) for s in self.voice]

        async def enqueue(db, job_type, payload, *a, **kw):
            self.jobs.append((job_type, dict(payload)))

        self.prev = registry.use(self.reg)
        self.s = Settings(CRM_LINK_ENABLED=True, ASTERISK_ENABLED=True,
                          CRM_LINK_SOFTPHONE_OPERATORS=KNOWN)
        self.s.__enter__()
        self.p = Patch((voice_client, "active_sessions", active), (queue, "enqueue", enqueue))
        self.p.__enter__()
        return self

    def __exit__(self, *exc):
        from app.integrations.retell import registry

        self.p.__exit__(*exc)
        self.s.__exit__(*exc)
        registry.use(self.prev)
        return False


def test_a_retell_call_is_listed_in_the_same_shape():
    print("GET /live-calls lists the Retell call:")
    from app.integrations.crm import api

    owen_voice = {"linkedid": "1759780000.99", "call_channel_id": "c2",
                  "media_channel_id": "m2", "bridge_id": "b2", "turns": 3, "duration_s": 10}
    started = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)
    with World(voice_sessions=[owen_voice]):
        out = asyncio.run(api.live_calls(
            db=FactsDb([(LINKEDID, started, CALLER, DID, "Receptionist")]), _key=None))
    calls = {c["linkedid"]: c for c in out["calls"]}
    check("both calls listed", set(calls) == {LINKEDID, "1759780000.99"})
    r = calls[LINKEDID]
    check("the Retell call says engine retell", r.get("engine") == "retell")
    check("with who, which line, which agent",
          (r["caller_number"], r["dialed_number"], r["agent"]) == (CALLER, DID, "Receptionist"))
    check("and how long it has run", isinstance(r["duration_s"], int) and r["duration_s"] >= 42)
    check("the owen_voice row keeps its exact shape (no engine key)",
          "engine" not in calls["1759780000.99"])
    text = repr(out)
    check("no channel, bridge or Retell id leaves OWEN",
          not any(x in text for x in (CHANNEL, "flow-dial-retell-1", "bridge-r1", "call_abc123")))


def _crm(fn_name):
    from app.integrations.crm import api

    return getattr(api, fn_name)(LINKEDID, api.LiveCallOperatorIn(operator_email=KNOWN),
                                 db=None, _key=None)


def test_listen_and_takeover_use_the_registry():
    print("Listen on a Retell call snoops the caller's channel:")
    with World() as w:
        asyncio.run(_crm("live_call_listen"))
        check("one monitor_listen on the caller's channel",
              [j[0] for j in w.jobs] == ["monitor_listen"]
              and w.jobs[0][1]["target_channel_id"] == CHANNEL)

    print("Take over a Retell call: the one existing mechanism, Retell's channels:")
    with World() as w:
        out = asyncio.run(_crm("live_call_takeover"))
        check("one monitor_takeover", [j[0] for j in w.jobs] == ["monitor_takeover"])
        job = w.jobs[0][1]
        check("the Retell SIP leg is the channel ejected",
              job["agent_channel_id"] == "flow-dial-retell-1")
        check("the operator joins the call's bridge", job["call_bridge_id"] == "bridge-r1")
        check("on the caller's channel", job["target_channel_id"] == CHANNEL)
        check("for the operator's slug", out["owner"] == KNOWN_SLUG)

    print("nothing live:")
    with World(retell_live=False) as w:
        from fastapi import HTTPException

        try:
            asyncio.run(_crm("live_call_takeover"))
            exc = None
        except HTTPException as e:
            exc = e
        check("404, nothing queued", exc is not None and exc.status_code == 404 and w.jobs == [])


def test_take_over_really_ejects_the_agent_leg():
    print("monitor.take_over: the agent's leg is NOT claimed, so its hangup is honoured:")
    from app.telephony import monitor, ownership

    class Ari:
        def __init__(self):
            self.ops = []

        async def hangup(self, ch):
            # What the real client does: refuse a channel that belongs to an owned call.
            if ownership.is_channel_owned(ch):
                self.ops.append(("REFUSED", ch))
                return
            self.ops.append(("hangup", ch))

        async def destroy_bridge(self, b):
            self.ops.append(("destroy_bridge", b))

        async def create_bridge(self):
            return "new-bridge"

        async def add_to_bridge(self, b, *chs):
            self.ops.append(("add", b, chs))
            return True

    ari = Ari()
    try:
        out = asyncio.run(monitor.take_over(
            ari, operator_id=KNOWN_SLUG, linkedid=LINKEDID, target_channel_id=CHANNEL,
            operator_channel_id="op-1", call_bridge_id="bridge-r1",
            agent_channel_id="flow-dial-retell-1"))
        check("the take-over succeeded", out["ok"] is True)
        check("the agent's leg was hung up, not refused",
              ("hangup", "flow-dial-retell-1") in ari.ops
              and ("REFUSED", "flow-dial-retell-1") not in ari.ops)
        check("the operator joined the call's bridge", ("add", "bridge-r1", ("op-1",)) in ari.ops)
        check("the caller is owned by the operator now", ownership.is_channel_owned(CHANNEL)
              and ownership.owner_of(LINKEDID) == KNOWN_SLUG)
        check("the caller's channel was never hung up", ("hangup", CHANNEL) not in ari.ops)
    finally:
        ownership.clear()


if __name__ == "__main__":
    test_a_retell_call_is_listed_in_the_same_shape()
    test_listen_and_takeover_use_the_registry()
    test_take_over_really_ejects_the_agent_leg()
    retell_guard.assert_untouched()
    print("\nALL RETELL SUPERVISION CHECKS PASSED")

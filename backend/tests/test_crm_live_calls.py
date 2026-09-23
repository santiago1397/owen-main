"""Supervising a live AI-agent call: OWEN's monitor role check, and the CRM's door to it.

The requirement (DECISIONS 2026-09-22, Q13): while an AI agent is on a call, the CRM shows
it and an ADMIN or DISPATCHER can Listen or Take over, on their own browser line. OWEN
already had the machinery; it had NO role check, and the CRM could not reach it.

What is asserted here, in the order it matters on a live phone system:

  1. **OWEN's own monitor routes need an admin.** A non-admin login is refused with a
     sentence; `stop` is deliberately left alone (it can only end the caller's own snoop).
  2. **The CRM routes refuse before anything happens.** Kill switch and dark telephony: no
     owen-voice lookup at all. An unprovisioned email: 403 by name, no session looked up,
     NOTHING queued — counted on a spy, not inferred from a status code.
  3. **A provisioned operator gets exactly the job OWEN's own button queues**, for the SLUG
     `ring.py` dials, with the agent's media channel and bridge taken from owen-voice and
     never from the request.
  4. **The live list says who, where, which agent and how long — and no channel ids.**
  5. **The refactor moved nothing**: OWEN's routes still queue the same payloads.

Run: python -m tests.test_crm_live_calls
"""

import asyncio
import inspect

KNOWN = "owen@dreamteamroofingfl.com"
KNOWN_SLUG = "owen-dreamteamroofingfl.com"
UNKNOWN = "stranger@example.com"
LINKEDID = "1758640000.42"
CUSTOMER = "+19415550123"
DID = "+19547758492"

SESSION = {
    "linkedid": LINKEDID,
    "session_uuid": "sess-1",
    "call_channel_id": "chan-caller",
    "media_channel_id": "chan-agent-media",
    "bridge_id": "bridge-call",
    "turns": 7,
    "duration_s": 83,
}


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"crm_live_calls failed at: {name}")


class World:
    """Settings, owen-voice and the job queue, faked and restored. Nothing here opens a
    socket, reaches owen-voice or touches a database."""

    FIELDS = ("CRM_LINK_ENABLED", "CRM_LINK_SOFTPHONE_OPERATORS", "ASTERISK_ENABLED")

    def __init__(self, sessions=None, **overrides):
        self.overrides = dict(CRM_LINK_ENABLED=True, CRM_LINK_SOFTPHONE_OPERATORS=KNOWN,
                              ASTERISK_ENABLED=True)
        self.overrides.update(overrides)
        self.sessions = [dict(SESSION)] if sessions is None else sessions
        self.lookups = 0
        self.jobs = []
        self.saved = {}

    def __enter__(self):
        from app.core.config import settings
        from app.services import queue
        from app.telephony import voice_client

        self.settings, self.queue, self.voice = settings, queue, voice_client
        for f in self.FIELDS:
            self.saved[f] = getattr(settings, f)
            setattr(settings, f, self.overrides[f])
        self.saved_active = voice_client.active_sessions
        self.saved_enqueue = queue.enqueue

        async def active_sessions():
            self.lookups += 1
            return [dict(s) for s in self.sessions]

        async def enqueue(db, job_type, payload, *a, **kw):
            self.jobs.append((job_type, dict(payload)))

        voice_client.active_sessions = active_sessions
        queue.enqueue = enqueue
        return self

    def __exit__(self, *exc):
        for f, v in self.saved.items():
            setattr(self.settings, f, v)
        self.voice.active_sessions = self.saved_active
        self.queue.enqueue = self.saved_enqueue
        return False


class FactsDb:
    """Answers the one `calls` query `supervision.call_facts` makes; any write fails."""

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def _write(self, *a, **kw):
        raise SystemExit("crm_live_calls failed at: listing live calls attempted a WRITE")

    add = add_all = delete = merge = _write

    async def commit(self):
        self._write()

    async def execute(self, stmt):
        self.statements.append(stmt)
        rows = self.rows

        class Result:
            def all(self_inner):
                return list(rows)
        return Result()


def _refusal(coro):
    from fastapi import HTTPException

    try:
        asyncio.run(coro)
    except HTTPException as exc:
        return exc
    return None


# --- 1. OWEN's own monitor routes -----------------------------------------------------------


def test_require_admin_refuses_a_non_admin_with_a_sentence():
    print("require_admin lets an admin through and refuses anybody else by name:")
    from app.api import deps

    class U:
        def __init__(self, role):
            self.role, self.email, self.active = role, "x@example.com", True

    check("an admin passes", asyncio.run(deps.require_admin(user=U("admin"))).role == "admin")
    check("case and spacing do not lock the owner out",
          asyncio.run(deps.require_admin(user=U(" Admin "))) is not None)
    for role in ("viewer", "dispatcher", "", None):
        exc = _refusal(deps.require_admin(user=U(role)))
        check(f"role {role!r} is refused with 403",
              exc is not None and exc.status_code == 403)
    check("the refusal is a sentence that says what is needed",
          "admin" in deps.REFUSE_NOT_ADMIN and " " in deps.REFUSE_NOT_ADMIN)
    check("the allowed roles are one module constant", deps.MONITOR_ROLES == {"admin"})


def test_the_monitor_routes_depend_on_require_admin_and_stop_does_not():
    print("active / listen / takeover need an admin; stop still needs only a login:")
    from app.api import telephony as tele
    from app.api.deps import current_user, require_admin

    for fn in (tele.monitor_active, tele.monitor_listen, tele.monitor_takeover):
        dep = inspect.signature(fn).parameters["user"].default
        check(f"{fn.__name__} depends on require_admin", dep.dependency is require_admin)
    dep = inspect.signature(tele.monitor_stop).parameters["user"].default
    check("monitor_stop still depends on current_user", dep.dependency is current_user)
    # The rest of the telephony surface is untouched: nothing else gained a role check.
    dep = inspect.signature(tele.webrtc_credentials).parameters["user"].default
    check("webrtc_credentials is untouched", dep.dependency is current_user)


def test_owens_routes_still_queue_the_same_jobs():
    print("the refactor onto telephony/supervision moved nothing in OWEN's own routes:")
    from app.api import telephony as tele

    class Admin:
        email, role, active = KNOWN, "admin", True

    with World() as w:
        out = asyncio.run(tele.monitor_listen(tele.MonitorIn(linkedid=LINKEDID),
                                              user=Admin(), db=None))
        check("listen queued one monitor_listen", [j[0] for j in w.jobs] == ["monitor_listen"])
        job = w.jobs[0][1]
        check("for the logged-in email, as before", job["operator_id"] == KNOWN)
        check("on the caller's channel from owen-voice",
              job["target_channel_id"] == "chan-caller")
        check("and answers the operator channel it queued",
              out["operator_channel"] == job["operator_channel_id"])

        w.jobs.clear()
        body = tele.TakeoverIn(linkedid=LINKEDID, operator_channel_id="op-existing",
                               snoop_channel_id="snoop-1", monitor_bridge_id="mb-1")
        out = asyncio.run(tele.monitor_takeover(body, user=Admin(), db=None))
        job = w.jobs[0][1]
        check("takeover queued one monitor_takeover",
              [j[0] for j in w.jobs] == ["monitor_takeover"])
        check("an already-listening operator's leg is reused",
              job["operator_channel_id"] == "op-existing")
        check("the snoop and monitor bridge are passed for teardown",
              job["snoop_channel_id"] == "snoop-1" and job["monitor_bridge_id"] == "mb-1")
        check("the agent's media channel comes from owen-voice",
              job["agent_channel_id"] == "chan-agent-media")
        check("the call bridge comes from owen-voice", job["call_bridge_id"] == "bridge-call")
        check("owner is the logged-in email", out["owner"] == KNOWN)

    with World(sessions=[]) as w:
        exc = _refusal(tele.monitor_listen(tele.MonitorIn(linkedid=LINKEDID),
                                           user=Admin(), db=None))
        check("a call that is not live is 404", exc is not None and exc.status_code == 404)
        check("and nothing was queued", w.jobs == [])


# --- 2 and 3. the CRM's listen / takeover ---------------------------------------------------


def _crm(fn_name, email=KNOWN, linkedid=LINKEDID):
    from app.integrations.crm import api as crm_api

    fn = getattr(crm_api, fn_name)
    return fn(linkedid, crm_api.LiveCallOperatorIn(operator_email=email), db=None, _key=None)


def test_the_crm_routes_refuse_before_anything_happens():
    for name in ("live_call_listen", "live_call_takeover"):
        print(f"{name} refuses before looking anything up or queueing anything:")
        with World(CRM_LINK_ENABLED=False) as w:
            exc = _refusal(_crm(name))
            check("kill switch: 503 naming the switch",
                  exc is not None and exc.status_code == 503
                  and "CRM_LINK_ENABLED" in str(exc.detail))
            check("...and owen-voice was never asked", w.lookups == 0)
            check("...and nothing was queued", w.jobs == [])

        with World(ASTERISK_ENABLED=False) as w:
            exc = _refusal(_crm(name))
            check("dark telephony: 503", exc is not None and exc.status_code == 503)
            check("...nothing looked up or queued", w.lookups == 0 and w.jobs == [])

        with World() as w:
            exc = _refusal(_crm(name, email=UNKNOWN))
            check("an unprovisioned email: 403", exc is not None and exc.status_code == 403)
            check("...saying they are not a provisioned operator",
                  "not a provisioned OWEN operator" in str(exc.detail))
            check("...no session looked up and NOTHING queued", w.lookups == 0 and w.jobs == [])

        with World(CRM_LINK_SOFTPHONE_OPERATORS="") as w:
            exc = _refusal(_crm(name))
            check("an empty roster grants nothing: 403",
                  exc is not None and exc.status_code == 403
                  and "CRM_LINK_SOFTPHONE_OPERATORS" in str(exc.detail))
            check("...nothing queued", w.jobs == [])

        with World() as w:
            exc = _refusal(_crm(name, email="   "))
            check("a blank email: 422", exc is not None and exc.status_code == 422)
            check("...nothing queued", w.jobs == [])

        with World(sessions=[]) as w:
            exc = _refusal(_crm(name))
            check("a call that has ended: 404", exc is not None and exc.status_code == 404)
            check("...nothing queued", w.jobs == [])


def test_a_provisioned_operator_gets_the_job_owens_button_queues():
    print("CRM listen rings the operator's OWN line, on a snoop of the live call:")
    with World() as w:
        out = asyncio.run(_crm("live_call_listen"))
        check("one monitor_listen", [j[0] for j in w.jobs] == ["monitor_listen"])
        job = w.jobs[0][1]
        check("for the SLUG the ring group dials, not the raw email",
              job["operator_id"] == KNOWN_SLUG)
        check("on the caller's channel from owen-voice",
              job["target_channel_id"] == "chan-caller")
        check("answers which operator was rung", out["operator"] == KNOWN_SLUG)

    print("CRM takeover seizes the call with owen-voice's channels, never the request's:")
    with World() as w:
        out = asyncio.run(_crm("live_call_takeover"))
        check("one monitor_takeover", [j[0] for j in w.jobs] == ["monitor_takeover"])
        job = w.jobs[0][1]
        check("for the slug", job["operator_id"] == KNOWN_SLUG)
        check("the agent's media channel is ejected",
              job["agent_channel_id"] == "chan-agent-media")
        check("the call bridge is the live one", job["call_bridge_id"] == "bridge-call")
        check("no snoop to tear down (the CRM cannot name one)",
              job["snoop_channel_id"] is None and job["monitor_bridge_id"] is None)
        check("owner is the slug", out["owner"] == KNOWN_SLUG)

    from app.integrations.crm import api as crm_api

    check("the request body has no channel field to point a takeover elsewhere",
          set(crm_api.LiveCallOperatorIn.model_fields) == {"operator_email"})


# --- 4. the live list -----------------------------------------------------------------------


def test_the_live_list_says_who_and_how_long_and_leaks_no_channel():
    print("GET /live-calls:")
    from datetime import datetime, timezone

    from app.integrations.crm import api as crm_api

    started = datetime(2026, 9, 23, 14, 5, tzinfo=timezone.utc)
    second = dict(SESSION, linkedid="1758640099.7", turns=1, duration_s=4)
    with World(sessions=[dict(SESSION), second]) as w:
        db = FactsDb([(LINKEDID, started, CUSTOMER, DID, "Intake")])
        out = asyncio.run(crm_api.live_calls(db=db, _key=None))
        calls = out["calls"]
        check("both live sessions are listed", len(calls) == 2)
        first = calls[0]
        check("in the documented shape", set(first) == {
            "linkedid", "caller_number", "dialed_number", "agent", "started_at",
            "duration_s", "turns"})
        check("who rang, which line, which agent", (first["caller_number"], first["dialed_number"],
              first["agent"]) == (CUSTOMER, DID, "Intake"))
        check("when it started, as ISO", first["started_at"] == started.isoformat())
        check("how long and how many turns, from owen-voice",
              first["duration_s"] == 83 and first["turns"] == 7)
        check("a session whose call row has not landed is still listed, unknowns None",
              calls[1]["linkedid"] == "1758640099.7" and calls[1]["caller_number"] is None)
        text = repr(out)
        check("no channel, bridge or session id leaves OWEN",
              not any(x in text for x in ("chan-caller", "chan-agent-media", "bridge-call",
                                          "sess-1")))
        check("one query for all of them", len(db.statements) == 1)

    with World(sessions=[]) as w:
        db = FactsDb([])
        out = asyncio.run(crm_api.live_calls(db=db, _key=None))
        check("nothing live is an empty list", out == {"calls": []})
        check("...without querying calls", db.statements == [])

    with World(CRM_LINK_ENABLED=False) as w:
        exc = _refusal(crm_api.live_calls(db=FactsDb([]), _key=None))
        check("kill switch: 503 and owen-voice never asked",
              exc is not None and exc.status_code == 503 and w.lookups == 0)


def test_all_three_are_crm_link_scoped():
    print("the three routes are gated by the CRM-link API key:")
    from app.core.apikeys import SCOPE_CRM_LINK
    from app.integrations.crm import api as crm_api

    for fn in (crm_api.live_calls, crm_api.live_call_listen, crm_api.live_call_takeover):
        inner = inspect.signature(fn).parameters["_key"].default.dependency
        closed = dict(zip(inner.__code__.co_freevars,
                          [c.cell_contents for c in (inner.__closure__ or ())]))
        check(f"{fn.__name__} is scope crm_link", closed.get("scope") == SCOPE_CRM_LINK)


if __name__ == "__main__":
    test_require_admin_refuses_a_non_admin_with_a_sentence()
    test_the_monitor_routes_depend_on_require_admin_and_stop_does_not()
    test_owens_routes_still_queue_the_same_jobs()
    test_the_crm_routes_refuse_before_anything_happens()
    test_a_provisioned_operator_gets_the_job_owens_button_queues()
    test_the_live_list_says_who_and_how_long_and_leaks_no_channel()
    test_all_three_are_crm_link_scoped()
    print("\nALL CRM LIVE-CALL CHECKS PASSED")

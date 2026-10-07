"""`/api/crm-link/numbers` (C5) and `/api/crm-link/agent-spend` (C6).

Driven through the real route functions against a fake session that holds rows and records
every write, so "nothing changed" is a count, not an inference.

Numbers:
  1. **The list** says which numbers a flow can answer and why the others cannot, and shows
     the assignment only while the number still runs the CRM-managed flow.
  2. **Assigning** builds a flow from the mode's template that `validate_graph` accepts —
     consent first, the agent, voicemail as the fallback — points the number at it, and
     remembers what was there before.
  3. **A hand-built flow is never replaced by accident**: 409 and nothing written, unless
     `replace: true`. **Removing** the assignment puts that flow back exactly, and archives
     (never deletes) the managed one.
  4. **Refusals** with a sentence: after_hours_ai without hours (no invented default), bad
     hours, nobody to ring, no consent notice, a number whose media is not on Asterisk, an
     unknown mode.
Spend:
  5. GET reports the cap in force (stored setting over env), the alert percent and the last
     24 hours' AI spend; PUT validates, stores, and wins over the env from then on.

Run: python -m tests.test_retell_numbers
"""

import asyncio
import uuid

from tests import retell_guard
from tests.retell_support import Patch, Settings
from tests.retell_support import check as _check


def check(name, cond):
    _check(name, cond, "retell_numbers")


class FakeDB:
    """Rows by (model, key). `get`, `add`, `delete`, `flush`, `commit`, and an `execute`
    that answers only the spend sum. Anything else fails loudly."""

    def __init__(self, *rows, spent=0):
        self.store: dict = {}
        self.adds: list = []
        self.deletes: list = []
        self.commits = 0
        self.spent = spent
        for r in rows:
            self._put(r)

    @staticmethod
    def _key(obj):
        return (type(obj).__name__, str(getattr(obj, "key", None) or obj.id))

    def _put(self, obj):
        self.store[self._key(obj)] = obj

    async def get(self, model, key):
        return self.store.get((model.__name__, str(key)))

    def add(self, obj):
        if getattr(obj, "id", None) is None and type(obj).__name__ != "AppSetting":
            obj.id = uuid.uuid4()
        self.adds.append(obj)
        self._put(obj)

    async def delete(self, obj):
        self.deletes.append(obj)
        self.store.pop(self._key(obj), None)

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    async def execute(self, stmt):
        spent = self.spent

        class R:
            def scalar_one(self_inner):
                return spent
        return R()

    def writes(self):
        return len(self.adds), len(self.deletes), self.commits


def _number(**kw):
    from app.models import Number

    base = dict(id=uuid.uuid4(), phone_number="+19545550199", friendly_name="Pilot line",
                media_provider="asterisk", owner_provider="bulkvs", active=True,
                released_at=None, provider_status="Active", flow_id=None)
    base.update(kw)
    return Number(**base)


class Agent:
    id = uuid.UUID("33333333-3333-3333-3333-333333333333")
    name = "Receptionist"


class World:
    def __init__(self, numbers=(), operators=("owen-desk",), agent_refusal=None, **settings):
        self.numbers = list(numbers)
        self.operators = list(operators)
        self.agent_refusal = agent_refusal
        self.settings = dict(CRM_LINK_ENABLED=True, BULKVS_MEDIA_PROVIDER="asterisk",
                             INBOUND_CONSENT_MEDIA="This call may be recorded.",
                             VOICEMAIL_GREETING="Please leave a message.",
                             AI_DAILY_SPEND_CAP_USD=0.0, AI_SPEND_ALERT_PCT=80)
        self.settings.update(settings)
        self.versions: dict = {}

    def __enter__(self):
        from app.integrations.crm import numbers as n

        async def all_numbers(db):
            return list(self.numbers)

        async def agent(db, name):
            if self.agent_refusal:
                raise n.Refused(*self.agent_refusal)
            return Agent()

        async def versions(db, flow_id):
            return [v.version for v in db.adds if type(v).__name__ == "FlowVersion"
                    and str(v.flow_id) == str(flow_id)]

        async def operators_for(db, number):
            return list(self.operators)

        self._s = Settings(**self.settings)
        self._s.__enter__()
        self._p = Patch((n, "_all_numbers", all_numbers), (n, "_agent", agent),
                        (n, "_versions", versions), (n, "_operators_for", operators_for))
        self._p.__enter__()
        return self

    def __exit__(self, *exc):
        self._p.__exit__(*exc)
        self._s.__exit__(*exc)
        return False


def _refusal(coro):
    from fastapi import HTTPException

    try:
        asyncio.run(coro)
    except HTTPException as exc:
        return exc
    return None


def _put(db, number, mode="ai_first", hours=None, replace=False, agent="Receptionist"):
    from app.integrations.crm import api

    body = api.NumberAssignmentIn(agent_name=agent, mode=mode, hours=hours, replace=replace)
    return api.assign_number(str(number.id), body, db=db, _key=None)


def _delete(db, number):
    from app.integrations.crm import api

    return api.unassign_number(str(number.id), db=db, _key=None)


def _graph(db):
    fvs = [a for a in db.adds if type(a).__name__ == "FlowVersion"]
    return fvs[-1].graph if fvs else None


# --- 1. the list -----------------------------------------------------------------------


def test_the_list_says_what_can_be_assigned_and_why_not():
    print("GET /numbers:")
    from app.integrations.crm import api

    good = _number()
    quo = _number(phone_number="+19415550100", media_provider=None, owner_provider="openphone")
    pending = _number(phone_number="+19415550101", provider_status="SUBMITTED")
    with World(numbers=[good, quo, pending]):
        out = asyncio.run(api.list_numbers(db=FakeDB(good, quo, pending), _key=None))
    rows = {r["e164"]: r for r in out["numbers"]}
    check("every number is listed", len(rows) == 3)
    check("in C5's shape", set(rows[good.phone_number]) == {
        "id", "e164", "label", "assignable", "reason", "assignment"})
    check("an Asterisk-media DID is assignable", rows[good.phone_number]["assignable"] is True
          and rows[good.phone_number]["reason"] is None)
    check("a number whose calls do not run through OWEN is not, and says why",
          rows[quo.phone_number]["assignable"] is False
          and "do not run through OWEN" in rows[quo.phone_number]["reason"])
    check("a pending port-in is not, and says why",
          "SUBMITTED" in rows[pending.phone_number]["reason"])
    check("nothing assigned yet", all(r["assignment"] is None for r in rows.values()))

    from app.integrations.crm.numbers import assignable_reason
    from datetime import datetime, timezone

    check("a released number says so", "released" in assignable_reason(
        media_provider="asterisk", expected_media="asterisk", active=False,
        released_at=datetime.now(timezone.utc), provider_status="Active"))


# --- 2 and 3. assign, protect, restore ----------------------------------------------------


def test_assign_builds_a_valid_flow_and_remembers_what_was_there():
    print("PUT ai_first on a number with no flow:")
    from app.flows.validator import validate_graph

    number = _number()
    db = FakeDB(number)
    with World(numbers=[number]):
        out = asyncio.run(_put(db, number))
        graph = _graph(db)
        check("a flow version was written and it validates", graph is not None
              and validate_graph(graph).ok)
        nodes = graph["nodes"]
        check("consent first", nodes["entry"]["next"]["default"] == "consent"
              and nodes["consent"]["media"] == "This call may be recorded.")
        check("then the agent, by id", nodes["consent"]["next"]["default"] == "agent"
              and nodes["agent"]["agent_id"] == str(Agent.id))
        check("failed and an undirected transfer go to voicemail",
              nodes["agent"]["next"]["failed"] == "voicemail"
              and nodes["agent"]["next"]["transfer"] == "voicemail")
        check("voicemail is the fallback, with the greeting",
              graph["default_fallback"] == "voicemail"
              and nodes["voicemail"]["greeting"] == "Please leave a message.")
        check("the number now runs that flow", str(number.flow_id) == out["flow_id"])
        flow = asyncio.run(db.get(__import__("app.models", fromlist=["Flow"]).Flow, out["flow_id"]))
        check("and the flow's active version is the one written",
              flow.active_version_id == [a for a in db.adds
                                         if type(a).__name__ == "FlowVersion"][-1].id)
        check("nothing was there before", out["previous_flow_id"] is None)
        check("the answer shows the assignment", out["assignment"] == {
            "agent_name": "Receptionist", "mode": "ai_first", "hours": None})

        out2 = asyncio.run(_put(db, number, mode="staff_then_ai"))
        check("re-assigning appends a version to the SAME managed flow",
              out2["flow_id"] == out["flow_id"]
              and [a.version for a in db.adds if type(a).__name__ == "FlowVersion"] == [1, 2])
        g2 = _graph(db)
        check("staff_then_ai rings the staff, then the agent",
              g2["nodes"]["consent"]["next"]["default"] == "ring"
              and g2["nodes"]["ring"]["operators"] == ["owen-desk"]
              and g2["nodes"]["ring"]["next"]["noanswer"] == "agent"
              and validate_graph(g2).ok)


def test_a_hand_built_flow_is_protected_and_restored():
    print("a number running a hand-built flow:")
    from app.models import Flow

    hand = Flow(id=uuid.uuid4(), name="Owner's flow", active_version_id=uuid.uuid4())
    number = _number(flow_id=hand.id)
    db = FakeDB(number, hand)
    with World(numbers=[number]):
        exc = _refusal(_put(db, number))
        check("PUT without replace: 409", exc is not None and exc.status_code == 409)
        check("naming the flow and the way out", "Owner's flow" in exc.detail
              and "replace" in exc.detail
              # the CRM tells this 409 from the other two by these words (ghl-clone
              # app/ai/api.py HAND_BUILT_NEEDLE) — change both or neither
              and "hand-built flow" in exc.detail)
        check("NOTHING written", db.writes() == (0, 0, 0) and number.flow_id == hand.id)

        out = asyncio.run(_put(db, number, replace=True))
        managed = out["flow_id"]
        check("with replace: true the number runs the CRM flow", str(number.flow_id) == managed)
        check("and the hand-built one is remembered", out["previous_flow_id"] == str(hand.id))

        asyncio.run(_put(db, number, mode="ai_first"))
        out = asyncio.run(_delete(db, number))
        check("DELETE puts the hand-built flow back", number.flow_id == hand.id)
        check("the managed flow is archived, not deleted",
              asyncio.run(db.get(Flow, managed)).archived_at is not None
              and not any(type(d).__name__ == "Flow" for d in db.deletes))
        check("the memory is gone and the answer shows no assignment",
              out["assignment"] is None and out["note"] is None)
        exc = _refusal(_delete(db, number))
        check("a second DELETE: 404", exc is not None and exc.status_code == 404)


def test_a_flow_changed_by_hand_since_is_left_alone():
    print("someone changed the number's flow by hand after the CRM assigned it:")
    from app.models import Flow

    number = _number()
    other = Flow(id=uuid.uuid4(), name="Changed by hand", active_version_id=uuid.uuid4())
    db = FakeDB(number, other)
    with World(numbers=[number]):
        asyncio.run(_put(db, number))
        number.flow_id = other.id
        out = asyncio.run(_delete(db, number))
        check("DELETE leaves the hand change in place", number.flow_id == other.id)
        check("and says so", "changed by hand" in (out["note"] or ""))


def test_refusals_say_why_and_write_nothing():
    number = _number()
    hours = {"tz": "America/New_York", "days": {"mon": [["08:00", "17:00"]],
                                                "Friday": [["08:00", "12:00"]]}}
    cases = [
        ("after_hours_ai without hours", dict(mode="after_hours_ai"), {}, 422, "no default"),
        ("hours with a bad time", dict(mode="after_hours_ai", hours={
            "tz": "America/New_York", "days": {"mon": [["8:00", "17:00"]]}}), {}, 422, "24-hour"),
        ("hours ending before they start", dict(mode="after_hours_ai", hours={
            "tz": "America/New_York", "days": {"mon": [["17:00", "08:00"]]}}), {}, 422,
         "ends before"),
        ("hours in no timezone", dict(mode="after_hours_ai", hours={
            "tz": "Mars/Olympus", "days": {"mon": [["08:00", "17:00"]]}}), {}, 422, "timezone"),
        ("an unknown day", dict(mode="after_hours_ai", hours={
            "tz": "America/New_York", "days": {"funday": [["08:00", "17:00"]]}}), {}, 422,
         "unknown day"),
        ("an unknown mode", dict(mode="robots_only"), {}, 422, "mode must be"),
        ("nobody to ring", dict(mode="staff_then_ai"), {"operators": ()}, 409, "nobody to ring"),
        ("no consent notice", dict(mode="ai_first"), {"INBOUND_CONSENT_MEDIA": ""}, 409,
         "consent"),
        ("an unknown agent", dict(mode="ai_first"),
         {"agent_refusal": (404, "the phone system has no agent named 'X'")}, 404, "no agent"),
    ]
    for label, kw, world, code, words in cases:
        print(f"refused: {label}:")
        db = FakeDB(number)
        with World(numbers=[number], **world):
            exc = _refusal(_put(db, number, **kw))
        check(f"{code}", exc is not None and exc.status_code == code)
        check(f"says '{words}'", words in str(exc.detail))
        check("nothing written", db.writes() == (0, 0, 0) and number.flow_id is None)

    print("a number whose calls are not on Asterisk:")
    quo = _number(media_provider=None)
    db = FakeDB(quo)
    with World(numbers=[quo]):
        exc = _refusal(_put(db, quo))
    check("409 with the reason", exc is not None and exc.status_code == 409
          and "do not run through OWEN" in exc.detail)

    print("after_hours_ai with good hours:")
    db = FakeDB(number)
    with World(numbers=[number]):
        out = asyncio.run(_put(db, number, mode="after_hours_ai", hours=hours))
    g = _graph(db)
    check("the hours node gets the schedule, days normalised",
          g["nodes"]["hours"]["hours"] == {"tz": "America/New_York", "schedule": {
              "mon": [["08:00", "17:00"]], "fri": [["08:00", "12:00"]]}})
    check("open rings the staff (then voicemail), closed goes to the agent",
          g["nodes"]["hours"]["next"] == {"open": "ring", "closed": "agent"}
          and g["nodes"]["ring"]["next"]["noanswer"] == "voicemail")
    check("the assignment keeps the hours as the CRM sent them",
          out["assignment"]["hours"] == hours)


def test_every_template_validates():
    print("every mode's template is a graph activation accepts:")
    from app.flows.validator import validate_graph
    from app.integrations.crm.numbers import MODES, build_graph

    for mode in MODES:
        g = build_graph(mode, agent_id="a", agent_name="A", consent="c", greeting="g",
                        operators=["op"], ring_timeout=25, record=True,
                        hours={"tz": "America/New_York", "schedule": {"mon": [["08:00", "17:00"]]}})
        r = validate_graph(g)
        check(f"{mode}: no errors", r.ok)
        check(f"{mode}: no unreachable nodes",
              not any("unreachable" in w for w in r.warnings))


# --- 5. spend ----------------------------------------------------------------------------


def test_spend_get_and_put():
    print("GET /agent-spend before anything is set: the env defaults:")
    from app.agents import spend
    from app.integrations.crm import api

    db = FakeDB(spent=12.3456)
    with World(AI_DAILY_SPEND_CAP_USD=40.0):
        out = asyncio.run(api.get_agent_spend(db=db, _key=None))
        check("C6's shape", set(out) == {"daily_cap_usd", "alert_pct", "today_usd"})
        check("the env cap and alert percent", (out["daily_cap_usd"], out["alert_pct"]) == (40.0, 80))
        check("today's spend, to the cent", out["today_usd"] == 12.35)

        print("PUT the pilot cap:")
        out = asyncio.run(api.put_agent_spend(api.AgentSpendIn(daily_cap_usd=25, alert_pct=80),
                                              db=db, _key=None))
        check("stored and committed", db.commits == 1 and asyncio.run(
            db.get(__import__("app.models", fromlist=["AppSetting"]).AppSetting,
                   spend.SETTING_KEY)).value == {"daily_cap_usd": 25.0, "alert_pct": 80})
        check("answered with the cap now in force", out["daily_cap_usd"] == 25.0)
        out = asyncio.run(api.get_agent_spend(db=db, _key=None))
        check("the stored setting wins over the env from now on", out["daily_cap_usd"] == 25.0)

        for bad, words in (((-1, 80), "zero"), ((25, 0), "between"), ((25, 101), "between"),
                           ((50000, 80), "not a cap")):
            before = db.commits
            exc = _refusal(api.put_agent_spend(
                api.AgentSpendIn(daily_cap_usd=bad[0], alert_pct=bad[1]), db=db, _key=None))
            check(f"{bad}: 422 saying '{words}', nothing stored",
                  exc is not None and exc.status_code == 422 and words in exc.detail
                  and db.commits == before)

    with World(CRM_LINK_ENABLED=False):
        exc = _refusal(api.get_agent_spend(db=FakeDB(), _key=None))
        check("the kill switch: 503", exc is not None and exc.status_code == 503)

    print("the alert threshold:")
    check("80% of $25 is $20", spend.should_alert(20.0, 25.0, 80)
          and not spend.should_alert(19.99, 25.0, 80))
    check("no cap, no alert", not spend.should_alert(1000, 0, 80))
    check("a stored nonsense value is ignored, not trusted",
          spend.effective_limits({"daily_cap_usd": "lots"}, env_cap=5, env_alert_pct=80)
          == (5.0, 80))


if __name__ == "__main__":
    test_the_list_says_what_can_be_assigned_and_why_not()
    test_assign_builds_a_valid_flow_and_remembers_what_was_there()
    test_a_hand_built_flow_is_protected_and_restored()
    test_a_flow_changed_by_hand_since_is_left_alone()
    test_refusals_say_why_and_write_nothing()
    test_every_template_validates()
    test_spend_get_and_put()
    retell_guard.assert_untouched()
    print("\nALL RETELL NUMBERS / SPEND CHECKS PASSED")

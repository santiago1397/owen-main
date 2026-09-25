"""`POST|GET /api/crm-link/agent-versions` — the CRM publishes the voice agent it edits.

Asserted by behaviour, against a fake database that records every row written:

  1. **A second push of the same CRM version makes no second version.** Same answer, same
     version id, the row count unchanged — and the active pointer where it was.
  2. **The agent is found by name and never created.** Unknown → 404 and no row of any kind;
     two agents with the name → 409 naming both, nothing written.
  3. **Validation is activation's.** A config `validate_agent_config` refuses (send_sms on
     owen_voice, knowledge over 6000) is refused with every problem and writes nothing.
  4. **activate=false appends without moving the live pointer.**
  5. **Same CRM version, different content** is refused rather than silently picking one.
  6. **Gated like the module**: scope `crm_link`, and the kill switch refuses before a read.
  7. **The GET is read-only** and returns each agent's ACTIVE config.
  8. **Answering calls** (phase 2c, 2026-09-25) — the CRM's own switch:
     * `activate: true` for a CRM version ALREADY stored activates that row and appends none;
     * `{"agent_name", "deactivate": true}` clears the active pointer, deletes nothing, and
       says what was answering; the two forms cannot be mixed;
     * every answer says what is answering NOW (`answering`, `active_version`);
     * the route's docstring says a deactivated agent sends the caller to the flow's
       fallback (the `failed` port), not a failed call — pinned, because the word
       "deactivate" invites the wrong reading.

Run: python -m tests.test_crm_agent_versions
"""

import asyncio
import inspect
import uuid


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"crm_agent_versions failed at: {name}")


GOOD = {
    "persona": "You are the Dream Team Roofing receptionist.",
    "greeting": "Thanks for calling Dream Team Roofing.",
    "model": "gpt-4o-mini",
    "voice": "aura-2-andromeda-en",
    "tts_provider": "deepgram",
    "engine": "owen_voice",
    "tools": {"capture_lead": True, "end_call": True},
    "knowledge": "We repair and replace roofs in Manatee and Sarasota counties.",
}


# --- a fake async session -------------------------------------------------------------------

class _Scalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return list(self.rows)


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return _Scalars(self.rows)


def _where_value(stmt):
    """The right-hand value of `select(X).where(X.col == value)`."""
    return stmt.whereclause.right.value


class FakeDB:
    """Holds agents and versions; answers the two SELECTs the module makes; records writes.

    Refuses anything it does not recognise, so a new query shows up as a failure here
    rather than as a test that silently stops exercising the code."""

    def __init__(self, agents=(), versions=()):
        self.agents = list(agents)
        self.versions = list(versions)
        self.pending = []
        self.commits = 0
        self.flushes = 0
        self.statements = []

    async def execute(self, stmt):
        from app.models import Agent, AgentVersion

        self.statements.append(stmt)
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Agent:
            if stmt.whereclause is None:
                return _Result(sorted(self.agents, key=lambda a: a.name))
            name = _where_value(stmt)
            return _Result([a for a in self.agents if a.name == name])
        if entity is AgentVersion:
            agent_id = _where_value(stmt)
            return _Result(sorted((v for v in self.versions if v.agent_id == agent_id),
                                  key=lambda v: v.version))
        raise AssertionError(f"unexpected query: {stmt}")

    async def get(self, model, pk):
        from app.models import AgentVersion

        if model is AgentVersion:
            return next((v for v in self.versions if v.id == pk), None)
        raise AssertionError(f"unexpected get: {model}")

    def add(self, obj):
        self.pending.append(obj)

    async def flush(self):
        self.flushes += 1
        for obj in self.pending:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()
            self.versions.append(obj)
        self.pending = []

    async def commit(self):
        await self.flush()
        self.commits += 1

    async def rollback(self):
        self.pending = []


def agent(name="Intake"):
    from app.models import Agent

    a = Agent(name=name)
    a.id = uuid.uuid4()
    a.active_version_id = None
    return a


def version(a, number, config):
    from app.models import AgentVersion

    v = AgentVersion(agent_id=a.id, version=number, config=config)
    v.id = uuid.uuid4()
    return v


def publish(db, **kw):
    from app.integrations.crm import agent_versions as av

    args = {"agent_name": "Intake", "config": GOOD, "crm_version": 7, "crm_agent_id": 3,
            "activate": True, **kw}
    return asyncio.run(av.publish(db, **args))


def refused(db, **kw):
    from app.integrations.crm import agent_versions as av

    try:
        publish(db, **kw)
    except av.Refused as r:
        return r
    return None


# --- 1. idempotency -------------------------------------------------------------------------

def test_a_second_push_of_the_same_crm_version_makes_no_second_version():
    print("publishing CRM version 7 twice:")
    a = agent()
    v1 = version(a, 1, {"persona": "old, hand-written"})
    a.active_version_id = v1.id
    db = FakeDB([a], [v1])

    first = publish(db)
    check("the first push appends version 2", first["created"] and first["version"] == 2)
    check("...and activates it", a.active_version_id == uuid.UUID(first["version_id"]))
    stored = [v for v in db.versions if v.version == 2][0].config
    check("the stored config records the CRM version", stored["crm_version"] == 7)
    check("...and the CRM agent", stored["crm_agent_id"] == 3)
    check("...and is otherwise exactly what the CRM sent",
          {k: v for k, v in stored.items() if not k.startswith("crm_")} == GOOD)

    rows_before = len(db.versions)
    second = publish(db)
    check("the second push appends NOTHING", len(db.versions) == rows_before == 2)
    check("it answers created: false", second["created"] is False)
    check("with the SAME version id and number",
          second["version_id"] == first["version_id"] and second["version"] == 2)
    check("the active version is still that one", second["active"] is True
          and a.active_version_id == uuid.UUID(first["version_id"]))

    third = publish(db, crm_version=8, config={**GOOD, "greeting": "Hello again."})
    check("a NEW CRM version does append", third["created"] and third["version"] == 3)


def test_a_retry_after_the_live_pointer_moved_does_not_resurrect_by_accident():
    """activate=false on a retry must not move the pointer either way."""
    print("activate=false appends without touching the live pointer:")
    a = agent()
    v1 = version(a, 1, {"persona": "live"})
    a.active_version_id = v1.id
    db = FakeDB([a], [v1])
    out = publish(db, activate=False)
    check("appended", out["created"] and len(db.versions) == 2)
    check("the live version did not change", a.active_version_id == v1.id)
    check("and it says so", out["active"] is False)


def test_the_same_crm_version_with_different_content_is_refused():
    print("CRM version 7 again, but different:")
    a = agent()
    db = FakeDB([a])
    publish(db)
    r = refused(db, config={**GOOD, "persona": "someone else's"})
    check("refused 409", r is not None and r.status == 409)
    check("naming the version", "CRM version 7" in r.message)
    check("nothing appended", len(db.versions) == 1)


# --- 2. never create an agent ---------------------------------------------------------------

def test_an_unknown_agent_is_404_and_nothing_is_created():
    print("an agent name owen-main does not have:")
    db = FakeDB([agent("Someone else")])
    r = refused(db)
    check("404", r is not None and r.status == 404)
    check("the sentence names it", "'Intake'" in r.message)
    check("no version written", db.versions == [] and db.flushes == 0 and db.commits == 0)
    check("no agent written either", db.pending == [] and len(db.agents) == 1)


def test_two_agents_with_one_name_is_refused_not_guessed():
    print("two agents called Intake:")
    a, b = agent(), agent()
    db = FakeDB([a, b])
    r = refused(db)
    check("409", r is not None and r.status == 409)
    check("naming both ids", str(a.id) in r.message and str(b.id) in r.message)
    check("nothing written", db.versions == [] and db.commits == 0)


# --- 3. validation is activation's ----------------------------------------------------------

def test_what_activation_refuses_is_refused_with_every_problem():
    print("a config the activation gate refuses:")
    a = agent()
    db = FakeDB([a])
    bad = {**GOOD, "tools": {"send_sms": True, "end_call": True}, "knowledge": "x" * 6001}
    r = refused(db, config=bad)
    check("422", r is not None and r.status == 422)
    check("send_sms on owen_voice is named",
          any("send_sms" in e and "owen_voice" in e for e in r.errors))
    check("the over-long knowledge is named with its length",
          any("6001" in e for e in r.errors))
    check("the message carries both", "send_sms" in r.message and "6001" in r.message)
    check("nothing written", db.versions == [] and db.commits == 0)
    check("the live pointer untouched", a.active_version_id is None)


# --- 4. the pure kernel ---------------------------------------------------------------------

def test_the_plan_checks_idempotency_before_validation():
    """A version already stored answers 'existing' even if today's rules would refuse it —
    a retried push must not flip from success to failure because the rules moved."""
    print("the kernel's order of questions:")
    from app.integrations.crm import agent_versions as av

    old = av.stamped({**GOOD, "knowledge": "y" * 7000}, 5, 3)
    p = av.plan([("v-id", 4, old)], {**GOOD, "knowledge": "y" * 7000}, 5, 3)
    check("an existing CRM version is recognised", p.action == "existing"
          and p.existing_id == "v-id" and p.existing_version == 4)
    p = av.plan([("v-id", 4, old)], GOOD, 6, 3)
    check("the next CRM version appends as version 5", p.action == "append" and p.version == 5)
    p = av.plan([], GOOD, 1, None)
    check("an agent id is optional", p.action == "append" and "crm_agent_id" not in p.config)


# --- 5. the gates ---------------------------------------------------------------------------

def _scope_of(fn):
    dep = inspect.signature(fn).parameters["_key"].default.dependency
    cells = dict(zip(dep.__code__.co_freevars, [c.cell_contents for c in dep.__closure__ or ()]))
    return cells.get("scope")


def test_both_routes_are_crm_link_scoped_and_refuse_while_the_link_is_off():
    print("gated like every CRM-initiated route:")
    from fastapi import HTTPException

    from app.core.apikeys import SCOPE_CRM_LINK
    from app.core.config import settings
    from app.integrations.crm import api as crm_api

    check("POST is crm_link", _scope_of(crm_api.publish_agent_version) == SCOPE_CRM_LINK)
    check("GET is crm_link", _scope_of(crm_api.list_active_agent_versions) == SCOPE_CRM_LINK)

    class NoDB:
        async def execute(self, *_a):
            raise AssertionError("read a database with the link off")

    saved = settings.CRM_LINK_ENABLED
    settings.CRM_LINK_ENABLED = False
    try:
        body = crm_api.AgentVersionIn(agent_name="Intake", config=GOOD, crm_version=1)
        for call in (lambda: crm_api.publish_agent_version(body, db=NoDB(), _key=None),
                     lambda: crm_api.list_active_agent_versions(db=NoDB(), _key=None)):
            try:
                asyncio.run(call())
                exc = None
            except HTTPException as e:
                exc = e
            check("503 before any read", exc is not None and exc.status_code == 503)
    finally:
        settings.CRM_LINK_ENABLED = saved


def test_the_route_answers_refusals_as_a_sentence_the_crm_can_show():
    print("the route's refusal shape:")
    from fastapi import HTTPException

    from app.core.config import settings
    from app.integrations.crm import api as crm_api

    saved = settings.CRM_LINK_ENABLED
    settings.CRM_LINK_ENABLED = True
    try:
        db = FakeDB([agent()])
        body = crm_api.AgentVersionIn(agent_name="Intake", crm_version=2,
                                      config={**GOOD, "tools": {"send_sms": True}})
        try:
            asyncio.run(crm_api.publish_agent_version(body, db=db, _key=None))
            exc = None
        except HTTPException as e:
            exc = e
        check("422", exc is not None and exc.status_code == 422)
        check("detail.message is the sentence", "send_sms" in exc.detail["message"])
        check("detail.errors lists the problems", len(exc.detail["errors"]) >= 1)
        ok = asyncio.run(crm_api.publish_agent_version(
            crm_api.AgentVersionIn(agent_name="Intake", crm_version=2, config=GOOD),
            db=db, _key=None))
        check("a good one is accepted", ok["ok"] and ok["created"] and ok["active"])
    finally:
        settings.CRM_LINK_ENABLED = saved
    try:
        crm_api.AgentVersionIn(agent_name="Intake", crm_version=2, config=GOOD,
                               agent_id="make-one")
        extra_refused = False
    except Exception:  # noqa: BLE001 - pydantic's ValidationError
        extra_refused = True
    check("an unknown key in the body is refused (no smuggled agent id)", extra_refused)


# --- 6. the read ----------------------------------------------------------------------------

def test_the_get_returns_each_agents_active_config_and_writes_nothing():
    print("GET lists the live configs:")
    from app.integrations.crm import agent_versions as av

    a, b = agent("Intake"), agent("Overflow")
    v1, v2 = version(a, 1, {"persona": "old"}), version(a, 2, GOOD)
    a.active_version_id = v2.id
    db = FakeDB([a, b], [v1, v2])
    out = asyncio.run(av.active_agents(db))
    check("both agents listed", [x["name"] for x in out] == ["Intake", "Overflow"])
    check("the ACTIVE version, not the latest or the first",
          out[0]["active_version"]["version"] == 2 and out[0]["active_version"]["config"] == GOOD)
    check("an agent with nothing active says so", out[1]["active_version"] is None)
    check("nothing written", db.commits == 0 and db.flushes == 0 and db.pending == [])


# --- 7. answering calls: activate a stored version, deactivate -------------------------------

def deactivate(db, name="Intake"):
    from app.integrations.crm import agent_versions as av

    return asyncio.run(av.deactivate(db, agent_name=name))


def test_activating_a_stored_crm_version_moves_the_pointer_and_appends_nothing():
    print("stored while not answering, then switched on:")
    a = agent()
    db = FakeDB([a])
    stored = publish(db, activate=False)
    check("stored as version 1", stored["created"] and stored["version"] == 1)
    check("...not active", stored["active"] is False and a.active_version_id is None)
    check("...and it says nothing is answering",
          stored["answering"] is False and stored["active_version"] is None)
    rows = len(db.versions)
    on = publish(db, activate=True)
    check("switching on appends NOTHING", len(db.versions) == rows == 1)
    check("it answers created: false with the same version",
          on["created"] is False and on["version_id"] == stored["version_id"])
    check("the stored row is now the active one",
          a.active_version_id == uuid.UUID(stored["version_id"]) and on["active"] is True)
    check("...and it says so", on["answering"] is True and on["active_version"] == 1)


def test_storing_a_new_version_says_what_is_still_answering():
    print("a new version stored while an older one answers:")
    a = agent()
    db = FakeDB([a])
    publish(db)                                        # CRM v7 → version 1, active
    out = publish(db, crm_version=8, config={**GOOD, "greeting": "Hi."}, activate=False)
    check("stored as version 2, not active", out["version"] == 2 and out["active"] is False)
    check("version 1 is still answering and the answer names it",
          out["answering"] is True and out["active_version"] == 1)


def test_deactivate_clears_the_pointer_and_deletes_nothing():
    print("deactivate:")
    a = agent()
    db = FakeDB([a])
    first = publish(db)
    rows = [(v.id, v.version, dict(v.config)) for v in db.versions]
    out = deactivate(db)
    check("no active version any more", a.active_version_id is None)
    check("it says what was answering", out["deactivated"] is True
          and out["previous_version"] == 1)
    check("and that nothing is now", out["answering"] is False and out["active_version"] is None)
    check("no version deleted or changed",
          [(v.id, v.version, dict(v.config)) for v in db.versions] == rows)
    again = deactivate(db)
    check("deactivating again is harmless", again["deactivated"] is False
          and a.active_version_id is None)
    on = publish(db, activate=True)
    check("switching back on re-activates the SAME stored row",
          on["created"] is False and on["version_id"] == first["version_id"]
          and a.active_version_id == uuid.UUID(first["version_id"]))


def test_deactivate_finds_the_agent_by_name_like_publish():
    print("deactivate an agent owen-main does not have, or has twice:")
    from app.integrations.crm import agent_versions as av

    for agents, status in (([agent("Other")], 404), ([agent(), agent()], 409)):
        db = FakeDB(agents)
        try:
            deactivate(db)
            r = None
        except av.Refused as e:
            r = e
        check(f"{status}", r is not None and r.status == status)
        check("nothing written", db.commits == 0
              and all(x.active_version_id is None for x in agents))


def test_the_two_forms_cannot_be_mixed():
    print("the route's two body forms:")
    from app.integrations.crm import api as crm_api

    def refused_body(**kw):
        try:
            crm_api.AgentVersionIn(**kw)
        except Exception:  # noqa: BLE001 - pydantic's ValidationError
            return True
        return False

    check("deactivate with only the name is accepted",
          not refused_body(agent_name="Intake", deactivate=True))
    check("deactivate with a config is refused",
          refused_body(agent_name="Intake", deactivate=True, config=GOOD))
    check("deactivate with a version is refused",
          refused_body(agent_name="Intake", deactivate=True, crm_version=3))
    check("deactivate with activate is refused",
          refused_body(agent_name="Intake", deactivate=True, activate=True))
    check("a publish without a config is refused",
          refused_body(agent_name="Intake", crm_version=3))
    check("a publish without a version is refused",
          refused_body(agent_name="Intake", config=GOOD))


def test_the_route_deactivates_and_says_the_caller_goes_to_the_fallback():
    print("the route, deactivating:")
    from app.core.config import settings
    from app.integrations.crm import api as crm_api

    saved = settings.CRM_LINK_ENABLED
    settings.CRM_LINK_ENABLED = True
    try:
        a = agent()
        db = FakeDB([a])
        publish(db)
        out = asyncio.run(crm_api.publish_agent_version(
            crm_api.AgentVersionIn(agent_name="Intake", deactivate=True), db=db, _key=None))
        check("through the route", out["deactivated"] is True and a.active_version_id is None)
    finally:
        settings.CRM_LINK_ENABLED = saved
    doc = " ".join((crm_api.publish_agent_version.__doc__ or "").split())
    check("the docstring says the `failed` port", "`failed` port" in doc)
    check("...and the fallback (voicemail)", "fallback" in doc and "voicemail" in doc)
    check("...and that calls do not fail", "does not make calls fail" in doc)


def test_a_deactivated_agent_is_what_the_runtime_treats_as_no_active_version():
    """The docstring's claim, checked against the code it describes."""
    print("the runtime's reading of 'no active version':")
    import app.flows.runtime as rt

    src = inspect.getsource(rt)
    check("the runtime resolves the ACTIVE version", "agent.active_version_id is None" in src)
    check("...and with none, takes the failed port",
          'has no active version' in src and 'return ("failed", {})' in src)


def main():
    test_a_second_push_of_the_same_crm_version_makes_no_second_version()
    test_a_retry_after_the_live_pointer_moved_does_not_resurrect_by_accident()
    test_the_same_crm_version_with_different_content_is_refused()
    test_an_unknown_agent_is_404_and_nothing_is_created()
    test_two_agents_with_one_name_is_refused_not_guessed()
    test_what_activation_refuses_is_refused_with_every_problem()
    test_the_plan_checks_idempotency_before_validation()
    test_both_routes_are_crm_link_scoped_and_refuse_while_the_link_is_off()
    test_the_route_answers_refusals_as_a_sentence_the_crm_can_show()
    test_the_get_returns_each_agents_active_config_and_writes_nothing()
    test_activating_a_stored_crm_version_moves_the_pointer_and_appends_nothing()
    test_storing_a_new_version_says_what_is_still_answering()
    test_deactivate_clears_the_pointer_and_deletes_nothing()
    test_deactivate_finds_the_agent_by_name_like_publish()
    test_the_two_forms_cannot_be_mixed()
    test_the_route_deactivates_and_says_the_caller_goes_to_the_fallback()
    test_a_deactivated_agent_is_what_the_runtime_treats_as_no_active_version()
    print("\nALL PASS")


if __name__ == "__main__":
    main()

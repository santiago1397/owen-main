"""An AI agent places ONE outbound call a person started (owner, 2026-10-15) — built, OFF.

Asserted by behaviour against a memory store (no database), a fake ARI, a mocked Retell (the
real request code runs) and an in-memory Retell registry. Nothing here opens a socket;
`tests/retell_guard.py` fails the module if anything tried to reach Retell.

  1. **Off by default.** `AGENT_OUTBOUND_ENABLED` is False out of the box; the CRM route
     answers 503 with the sentence, the CLI refuses the same way, and NOTHING happens — no
     store read, nothing queued, no ARI operation, no Retell request.
  2. **Every guard refuses with a sentence and does nothing**: telephony off, no Retell key,
     no / long reason, not a US number, one of our own DIDs, an unknown agent, no active
     version, not a Retell agent, a from-number that is not ours / not active, no
     from-number to use, a blocked number, outside 08:00-20:00 New York, the daily cap, the
     same number within 10 minutes, the spend cap. The CLI also refuses without --yes.
  3. **The from-number**: given > the ONE CRM-assigned number > AGENT_OUTBOUND_FROM; never
     invented.
  4. **A queued call** is counted and is ONE job, with a dash-free `agentout...` linkedid.
  5. **Answered**: the PSTN leg is originated with our DID as caller ID and the X_OWEN_*
     variables; Retell is registered OUTBOUND (from our DID to the person) with
     `call_direction`, `call_reason` and the outbound greeting on top of the usual brief;
     bridged, the bridge recorded, the CRM told an OUTBOUND call with the same ai_call shape;
     the person's leg hung up at the end.
  6. **No answer / busy**: NO Retell registration; the CRM gets an outbound call with that
     status and who would have called and why.
  7. **Inbound unchanged**, the consumer and the projection know the new leg, the CRM can
     read the status, and nothing in the app queues the job but `place()`.

Run: python -m tests.test_agent_outbound_call
"""

import asyncio
import io
import json
import uuid
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx

from tests import retell_guard
from tests.retell_support import (DID, KEY, FakeAri, MemoryRegistry, Patch, Settings,
                                  leg_gone)
from tests.retell_support import check as _check


def check(name, cond):
    _check(name, cond, "agent_outbound_call")


BOSS = "+19415550188"
OTHER = "+19415550177"
AGENT = "Anisa"
AGENT_ID = "22222222-2222-2222-2222-222222222222"
# 15:00 UTC on a Wednesday in October = 11:00 in New York.
NOON_NY = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)

ON = dict(AGENT_OUTBOUND_ENABLED=True, ASTERISK_ENABLED=True, RETELL_API_KEY=KEY,
          AGENT_OUTBOUND_FROM="", AGENT_OUTBOUND_DAILY_MAX=10,
          AGENT_OUTBOUND_REPEAT_MINUTES=10, BULKVS_MEDIA_PROVIDER="asterisk")


class MemoryStore:
    """`outbound_call.DbStore` in memory. Records every touch, so "did nothing" is counted."""

    def __init__(self, *, agent=None, numbers=None, assigned=(), blocked=(), log=None):
        self.agent_row = agent if agent is not None else {
            "id": AGENT_ID, "name": AGENT, "active": True, "version": 4, "engine": "retell",
            "retell_agent_id": "agent_7f3a"}
        self.numbers = numbers if numbers is not None else {
            DID: {"phone_number": DID, "media_provider": "asterisk", "active": True,
                  "released_at": None, "provider_status": "Active"}}
        self.assigned = list(assigned)
        self.blocked_numbers = set(blocked)
        self.entries = list(log or [])
        self.touched: list[str] = []
        self.recorded: list[tuple] = []

    async def agent(self, name):
        from app.agents.outbound_call import Refused

        self.touched.append("agent")
        if self.agent_row is None or name != self.agent_row["name"]:
            raise Refused(404, f"the phone system has no agent named '{name}'")
        return dict(self.agent_row)

    async def number(self, e164):
        self.touched.append("number")
        row = self.numbers.get(e164)
        return dict(row) if row else None

    async def assigned_numbers(self, agent_id, agent_name):
        self.touched.append("assigned")
        return list(self.assigned)

    async def blocked(self, e164):
        self.touched.append("blocked")
        return e164 in self.blocked_numbers

    async def log(self, *, lock=False):
        self.touched.append("log")
        return [dict(e) for e in self.entries]

    async def record(self, entries, payload):
        self.recorded.append((entries, payload))
        self.entries = list(entries)


def _req(**kw):
    from app.agents.outbound_call import CallRequest

    base = dict(to_number=BOSS, agent_name=AGENT, reason="your roof inspection on Friday",
                from_number="")
    base.update(kw)
    return CallRequest(**base)


def _place(store, req=None, *, now=NOON_NY, over_cap=False, **settings):
    """place() with everything faked. Returns (result | Refused, store)."""
    from app.agents import outbound_call

    async def cap():
        return over_cap

    with Settings(**{**ON, **settings}), Patch((outbound_call, "_over_cap", cap)):
        try:
            return asyncio.run(outbound_call.place(store, req or _req(), now=now)), store
        except outbound_call.Refused as r:
            return r, store


# --- 1. off by default ---------------------------------------------------------------------


def test_off_by_default_and_nothing_happens():
    print("off by default:")
    from app.agents import outbound_call
    from app.core.config import Settings as AppSettings

    check("AGENT_OUTBOUND_ENABLED defaults to False",
          AppSettings.model_fields["AGENT_OUTBOUND_ENABLED"].default is False)
    store = MemoryStore()
    out, _ = _place(store, AGENT_OUTBOUND_ENABLED=False)
    check("place() refuses with 503 and the sentence",
          isinstance(out, outbound_call.Refused) and out.status == 503
          and out.message == outbound_call.OFF)
    check("...before reading anything", store.touched == [] and store.recorded == [])

    # The CRM route: 503, the sentence, and no session work, nothing queued.
    from fastapi import HTTPException

    from app.integrations.crm import api as crm_api
    from app.services import queue

    queued = []

    async def spy(*a, **kw):
        queued.append(a)

    class ExplodingDb:
        def __getattr__(self, name):
            raise AssertionError(f"the database was touched ({name})")

    body = crm_api.AgentCallIn(to_number=BOSS, agent_name=AGENT, reason="a test")
    with Settings(CRM_LINK_ENABLED=True, CRM_LINK_ALLOW_ANY_DESTINATION=True,
                  AGENT_OUTBOUND_ENABLED=False), Patch((queue, "enqueue", spy)):
        try:
            asyncio.run(crm_api.place_agent_call(body, db=ExplodingDb(), _key=None))
            got = None
        except HTTPException as exc:
            got = exc
    check("POST /api/crm-link/agent-calls answers 503",
          got is not None and got.status_code == 503)
    check("with the sentence the CRM shows",
          got is not None and got.detail == "Outbound AI calls are switched off on the phone "
                                            "system (AGENT_OUTBOUND_ENABLED)")
    check("and nothing was queued", queued == [])

    # The CLI: the same sentence, exit 1, no session opened (even WITH --yes).
    from app import db as app_db

    def no_session(*a, **kw):
        raise AssertionError("a database session was opened")

    buf = io.StringIO()
    with Settings(AGENT_OUTBOUND_ENABLED=False), Patch((app_db, "SessionLocal", no_session)), \
            redirect_stdout(buf):
        code = outbound_call.main(["--to", BOSS, "--agent", AGENT, "--reason", "a test",
                                   "--yes"])
    check("the CLI refuses with exit 1", code == 1)
    check("...saying why", outbound_call.OFF in buf.getvalue())

    # Switched on but no --yes: refused, exit 2, nothing opened.
    buf = io.StringIO()
    with Settings(AGENT_OUTBOUND_ENABLED=True), Patch((app_db, "SessionLocal", no_session)), \
            redirect_stdout(buf):
        code = outbound_call.main(["--to", BOSS, "--agent", AGENT, "--reason", "a test"])
    check("without --yes the CLI refuses with exit 2", code == 2)
    check("...and says nothing was dialled", "Nothing was dialled" in buf.getvalue())


# --- 2. the guards -------------------------------------------------------------------------


def test_every_guard_refuses_and_does_nothing():
    print("every guard refuses with a sentence, and nothing is queued:")
    from app.agents import outbound_call

    def refused(label, out, store, status, words):
        ok = (isinstance(out, outbound_call.Refused) and out.status == status
              and words in out.message and store.recorded == [])
        check(f"{label}: {status}, '{words}', nothing queued", ok)

    out, st = _place(MemoryStore(), ASTERISK_ENABLED=False)
    refused("telephony off", out, st, 503, "telephony is not enabled")
    out, st = _place(MemoryStore(), RETELL_API_KEY="")
    refused("no Retell key", out, st, 503, "RETELL_API_KEY")
    out, st = _place(MemoryStore(), _req(reason="   "))
    refused("no reason", out, st, 422, "say what the call is about")
    out, st = _place(MemoryStore(), _req(reason="x" * 301))
    refused("a 301-character reason", out, st, 422, "at most 300")
    for bad in ("911", "+4420794600", "+1941555018", "+10415550188", "hello"):
        out, st = _place(MemoryStore(), _req(to_number=bad))
        refused(f"to {bad!r}", out, st, 422, "full US number")
    out, st = _place(MemoryStore(), _req(to_number=DID))
    refused("to one of our own DIDs", out, st, 422, "one of our own numbers")
    out, st = _place(MemoryStore(), _req(agent_name="Nobody"))
    refused("an unknown agent", out, st, 404, "no agent named 'Nobody'")
    store = MemoryStore()
    store.agent_row["active"] = False
    out, st = _place(store)
    refused("no active version", out, st, 409, "no active version")
    store = MemoryStore()
    store.agent_row["engine"] = "owen_voice"
    out, st = _place(store)
    refused("not a Retell agent", out, st, 409, "not a Retell agent")
    out, st = _place(MemoryStore(), _req(from_number=OTHER))
    refused("from a number that is not ours", out, st, 409, "not one of our numbers")
    store = MemoryStore()
    store.numbers[DID]["active"] = False
    out, st = _place(store, _req(from_number=DID))
    refused("from a number that is switched off", out, st, 409, "switched off")
    store = MemoryStore()
    store.numbers[DID]["provider_status"] = "SUBMITTED"
    out, st = _place(store, _req(from_number=DID))
    refused("from a number BulkVS does not report Active", out, st, 409, "not Active")
    out, st = _place(MemoryStore())
    refused("no assigned number and no AGENT_OUTBOUND_FROM", out, st, 422,
            "is not assigned to a number")
    out, st = _place(MemoryStore(assigned=[DID, "+19545550100"]))
    refused("two assigned numbers and no default", out, st, 422, "answers 2 numbers")
    out, st = _place(MemoryStore(blocked=[BOSS]), _req(from_number=DID))
    refused("a blocked number", out, st, 409, "blocked")

    # Calling hours: 08:00 <= New York < 20:00 (EDT in October: UTC-4).
    for label, when, ok in (
            ("07:59", datetime(2026, 10, 14, 11, 59, tzinfo=timezone.utc), False),
            ("08:00", datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc), True),
            ("19:59", datetime(2026, 10, 14, 23, 59, tzinfo=timezone.utc), True),
            ("20:00", datetime(2026, 10, 15, 0, 0, tzinfo=timezone.utc), False),
            ("03:00", datetime(2026, 10, 14, 7, 0, tzinfo=timezone.utc), False)):
        out, st = _place(MemoryStore(), _req(from_number=DID), now=when)
        if ok:
            check(f"{label} New York is inside calling hours",
                  not isinstance(out, outbound_call.Refused) and len(st.recorded) == 1)
        else:
            refused(f"{label} New York", out, st, 409, "08:00-20:00 America/New_York")

    # The daily cap counts calls on the New York calendar day.
    today = [{"at": NOON_NY.replace(hour=13, minute=i).isoformat(), "to": f"+1941555{i:04d}"}
             for i in range(10)]
    out, st = _place(MemoryStore(log=today), _req(from_number=DID))
    refused("the 11th call of the day", out, st, 429, "AGENT_OUTBOUND_DAILY_MAX")
    yesterday = [{"at": datetime(2026, 10, 13, 15, i, tzinfo=timezone.utc).isoformat(),
                  "to": f"+1941555{i:04d}"} for i in range(10)]
    out, st = _place(MemoryStore(log=yesterday), _req(from_number=DID))
    check("yesterday's ten do not count today",
          not isinstance(out, outbound_call.Refused) and len(st.recorded) == 1)
    out, st = _place(MemoryStore(log=today[:3]), _req(from_number=DID),
                     AGENT_OUTBOUND_DAILY_MAX=3)
    refused("a lowered cap", out, st, 429, "placed 3 calls today")

    # The same number within ten minutes.
    nine = [{"at": datetime(2026, 10, 14, 14, 51, tzinfo=timezone.utc).isoformat(), "to": BOSS}]
    out, st = _place(MemoryStore(log=nine), _req(from_number=DID))
    refused("the same number 9 minutes later", out, st, 429, "less than 10 minutes ago")
    eleven = [{"at": datetime(2026, 10, 14, 14, 49, tzinfo=timezone.utc).isoformat(),
               "to": BOSS}]
    out, st = _place(MemoryStore(log=eleven), _req(from_number=DID))
    check("the same number 11 minutes later is allowed",
          not isinstance(out, outbound_call.Refused) and len(st.recorded) == 1)
    out, st = _place(MemoryStore(log=nine), _req(to_number=OTHER, from_number=DID))
    check("another number 9 minutes later is allowed",
          not isinstance(out, outbound_call.Refused) and len(st.recorded) == 1)

    out, st = _place(MemoryStore(), _req(from_number=DID), over_cap=True)
    refused("the spend cap", out, st, 409, "spend cap")


# --- 3 and 4. the from-number and a queued call --------------------------------------------


def test_the_from_number_and_a_queued_call():
    print("the from-number, and what a queued call is:")
    from app.agents import outbound_call

    out, st = _place(MemoryStore(assigned=[DID]))
    check("the ONE number the CRM assigned the agent is used",
          not isinstance(out, outbound_call.Refused) and out["from_number"] == DID)
    out, st = _place(MemoryStore(), AGENT_OUTBOUND_FROM=DID)
    check("else AGENT_OUTBOUND_FROM", not isinstance(out, outbound_call.Refused)
          and out["from_number"] == DID)
    out, st = _place(MemoryStore(assigned=[DID, "+19545550100"]), AGENT_OUTBOUND_FROM=DID)
    check("several assigned numbers fall back to AGENT_OUTBOUND_FROM",
          not isinstance(out, outbound_call.Refused) and out["from_number"] == DID)

    out, st = _place(MemoryStore(), _req(from_number=DID, to_number="(941) 555-0188",
                                         customer_first_name="  Owen ",
                                         requested_by="owen@dreamteamroofingfl.com"))
    check("202 body: dialing, with the call id = the linkedid",
          out["status"] == "dialing" and out["call_id"] == out["linkedid"])
    lid = out["linkedid"]
    check("the linkedid is the agent-outbound prefix + hex, with NO dash (recordings)",
          lid.startswith("agentout") and "-" not in lid)
    check("exactly one job's payload written", len(st.recorded) == 1)
    entries, payload = st.recorded[0]
    check("the number is normalised to E.164", payload["to_number"] == BOSS)
    check("the payload carries what the worker needs",
          payload == {"linkedid": lid, "to_number": BOSS, "from_number": DID,
                      "agent_id": AGENT_ID, "agent_name": AGENT, "version": 4,
                      "reason": "your roof inspection on Friday", "first_name": "Owen"})
    check("the attempt is counted, with who asked",
          entries[-1]["to"] == BOSS and entries[-1]["linkedid"] == lid
          and entries[-1]["by"] == "owen@dreamteamroofingfl.com")

    # The real store queues exactly one `agent_outbound_call` job in the same commit.
    from app.services import queue

    jobs = []

    async def enqueue(db, job_type, payload, delay_seconds=0):
        jobs.append((job_type, payload))

    class Db:
        def __init__(self):
            self.added = []

        async def get(self, _model, _key, **_kw):
            return None

        def add(self, row):
            self.added.append(row)

    db = Db()
    with Patch((queue, "enqueue", enqueue)):
        asyncio.run(outbound_call.DbStore(db).record([{"to": BOSS}], {"linkedid": "x"}))
    check("DbStore.record writes the log row and queues ONE agent_outbound_call job",
          jobs == [("agent_outbound_call", {"linkedid": "x"})] and len(db.added) == 1)


# --- 5. answered ---------------------------------------------------------------------------


CONFIG = {"engine": "retell", "retell_agent_id": "agent_7f3a",
          "tools": {"transfer": True, "end_call": True},
          "transfer_targets": {"office": {"kind": "number", "target": "+19549147244"}}}
KNOWN = {"known": True, "source": "crm", "contact": {"first_name": "Maria",
                                                     "last_name": "Ruiz"}}


class OutAri(FakeAri):
    """FakeAri plus the PSTN originate of an agent's outbound call."""

    def __init__(self, dial="answered", dial_info=None, **kw):
        super().__init__(**kw)
        self.dial = dial
        self.dial_info = dial_info or {}
        self.pstn = None

    async def originate_agent_outbound(self, queue, channel_id, number, *, caller_id,
                                       timeout_s, variables=None):
        self.ops.append(("originate_pstn", number))
        self.pstn = {"channel_id": channel_id, "number": number, "caller_id": caller_id,
                     "timeout_s": timeout_s, "variables": dict(variables or {})}
        return self.dial, dict(self.dial_info)


class FakeDb:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        return None

    async def get(self, _model, _id):
        return SimpleNamespace(name=AGENT)


def _payload(**kw):
    p = {"linkedid": "agentout" + uuid.uuid4().hex, "to_number": BOSS, "from_number": DID,
         "agent_id": AGENT_ID, "agent_name": AGENT, "version": 4,
         "reason": "your roof inspection on Friday", "first_name": ""}
    p.update(kw)
    return p


def _run_call(ari, payload, *, brief=KNOWN):
    """outbound_call.run_call with the database, Retell and the CRM faked. Returns
    (outcome, Retell bodies, reports from the runtime, unanswered CRM facts)."""
    from app.agents import outbound_call
    from app.agents import retell as engine
    from app.agents import spend
    from app.agents.retell import RetellVoiceAgentSession
    from app.flows import runtime
    from app.integrations.crm import push as crm_push
    from app.integrations.retell import client, functions, registry

    bodies, reports, facts = [], [], []

    def handler(request: httpx.Request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"call_id": f"call_{len(bodies)}"})

    version = SimpleNamespace(id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
                              agent_id=uuid.UUID(AGENT_ID), config=CONFIG, version=4)

    async def resolve(_db, _agent_id):
        return version

    async def nothing(*_a, **_kw):
        return None

    async def report_crm(**kw):
        reports.append(kw)

    async def cap():
        return False

    async def fetch_brief(_spec, _ctx):
        return brief

    async def version_config(_vid):
        return dict(CONFIG)

    async def provider_id():
        return 1

    async def artifacts(_db, _lid):
        return "owen-call-7", None, None

    async def enqueue_call_event(f):
        facts.append(f)
        return True

    reg = MemoryRegistry()
    previous = registry.use(reg)
    try:
        with Settings(RETELL_API_KEY=KEY, RETELL_SIP_ENDPOINT="retell",
                      RETELL_SIP_HOST="sip.retellai.com", RETELL_CONNECT_TIMEOUT_SECONDS=2,
                      AGENT_OUTBOUND_RING_SECONDS=30), \
                Patch((client, "TRANSPORT", httpx.MockTransport(handler)),
                      (spend, "over_cap", cap), (spend, "check_alert", nothing),
                      (engine, "_fetch_brief", fetch_brief), (engine, "POLL_SECONDS", 0.01),
                      (engine, "EXIT_GRACE_SECONDS", 0.0),
                      (functions, "_version_config", version_config),
                      (runtime, "SessionLocal", FakeDb),
                      (runtime, "_resolve_active_agent_version", resolve),
                      (runtime, "_pin_agent_version", nothing),
                      (runtime, "_store_agent_output", nothing),
                      (runtime, "_enqueue_crm_report", nothing),
                      (runtime, "_report_agent_call_to_crm", report_crm),
                      (runtime, "get_session_for_agent",
                       lambda _spec: RetellVoiceAgentSession()),
                      (outbound_call, "_provider_id", provider_id),
                      (crm_push, "call_artifacts", artifacts),
                      (crm_push, "enqueue_call_event", enqueue_call_event)):
            import app.db as app_db

            with Patch((app_db, "SessionLocal", FakeDb)):
                outcome = asyncio.run(outbound_call.run_call(ari, payload))
    finally:
        registry.use(previous)
    return outcome, bodies, reports, facts, reg


def _retell_hangs_up(after=0.03):
    async def script(ari):
        from app.flows import dtmf

        await asyncio.sleep(after)
        dtmf.push_channel_event(ari.out_id, leg_gone(ari.out_id))
    return script


def test_an_answered_call_runs_the_agent_outbound():
    print("the person answers; the agent talks to them:")
    from app.integrations.retell.brief import DISCLOSURE_RULE

    ari = OutAri(script=_retell_hangs_up())
    payload = _payload()
    lid = payload["linkedid"]
    outcome, bodies, reports, facts, reg = _run_call(ari, payload)

    p = ari.pstn
    check("the person's number is rung, as the call's own (Linkedid) leg",
          p["number"] == BOSS and p["channel_id"] == lid)
    check("with OUR DID as caller ID (BulkVS refuses anything else)", p["caller_id"] == DID)
    check("ringing for AGENT_OUTBOUND_RING_SECONDS", p["timeout_s"] == 30)
    check("marked outbound, from our DID, to the person (the projection files it so)",
          p["variables"] == {"X_OWEN_DIRECTION": "outbound", "X_OWEN_FROM": DID,
                             "X_OWEN_TO": BOSS})
    check("the PSTN leg is dialled BEFORE Retell is asked", ari.names()[0] == "originate_pstn")

    check("exactly one Retell registration", len(bodies) == 1)
    b = bodies[0]
    check("registered OUTBOUND, from our DID, to the person",
          (b["direction"], b["from_number"], b["to_number"]) == ("outbound", DID, BOSS))
    check("metadata says outbound, with the linkedid, agent and version",
          b["metadata"] == {"linkedid": lid, "owen_agent": AGENT, "owen_version": 4,
                            "outbound": True})
    v = b["retell_llm_dynamic_variables"]
    check("the usual seven variables plus call_direction and call_reason",
          set(v) == {"customer_known", "customer_first_name", "customer_brief",
                     "caller_number", "dialed_number", "greeting", "transfer_failed",
                     "call_direction", "call_reason"})
    check("call_direction = outbound, call_reason = the reason",
          v["call_direction"] == "outbound"
          and v["call_reason"] == "your roof inspection on Friday")
    check("the brief is the CRM's for the number called, rule first",
          v["customer_brief"].startswith(DISCLOSURE_RULE) and v["customer_known"] == "yes"
          and v["caller_number"] == BOSS and v["dialed_number"] == DID)
    check("the greeting: who, the notice, the name, the reason",
          v["greeting"] == "Hi, this is Anisa with Dream Team Roofing. Just so you know, this "
                           "call's recorded and I'm an AI assistant. Is this Maria? I'm calling "
                           "about your roof inspection on Friday.")

    check("Retell's SIP leg is dialled", ari.endpoint == "PJSIP/retell/sip:call_1@sip.retellai.com")
    adds = [o for o in ari.ops if o[0] == "add_to_bridge"]
    check("the person and Retell are bridged", adds and set(adds[0][2]) == {lid, ari.out_id})
    check("the bridge is recorded as <linkedid>-agent-1",
          ("record_bridge", "bridge-1", f"{lid}-agent-1") in ari.ops)
    row = reg.rows["call_1"]
    check("the live registry row (live calls, Listen / Take over) is on the person's leg",
          row["linkedid"] == lid and row["call_channel_id"] == lid
          and row["caller_number"] == BOSS and row["dialed_number"] == DID)
    check("Retell hung up -> end_call", outcome == "end_call")

    check("the CRM is told once, as an OUTBOUND call", len(reports) == 1
          and reports[0]["direction"] == "outbound")
    r = reports[0]
    check("on the called number's thread, from our line",
          r["caller_number"] == BOSS and r["dialed"] == DID and r["agent_name"] == AGENT)
    extra = r["data"]["ai_call_extra"]
    check("the same ai_call shape, plus direction and reason",
          extra["engine"] == "retell" and extra["retell_call_id"] == "call_1"
          and extra["direction"] == "outbound"
          and extra["reason"] == "your roof inspection on Friday")
    check("no 'unanswered' report on an answered call", facts == [])
    check("the person's leg is hung up when the agent is done", ("hangup", lid) in ari.ops)

    # Unknown number: no name in the greeting.
    ari = OutAri(script=_retell_hangs_up())
    _o, bodies, *_ = _run_call(ari, _payload(), brief={"known": False})
    check("an unknown number: no 'Is this ...?'",
          bodies[0]["retell_llm_dynamic_variables"]["greeting"].endswith(
              "I'm an AI assistant. I'm calling about your roof inspection on Friday."))
    ari = OutAri(script=_retell_hangs_up())
    _o, bodies, *_ = _run_call(ari, _payload(first_name="Owen"), brief={"known": False})
    check("...unless the person who started it gave a first name",
          "Is this Owen? I'm calling about" in bodies[0]["retell_llm_dynamic_variables"][
              "greeting"])


def test_the_runtime_reports_an_outbound_call_to_the_crm():
    print("the runtime's CRM report says OUTBOUND, same body otherwise:")
    from app.flows import runtime
    from app.integrations.crm import push as crm_push
    from app.integrations.crm.events import to_crm_event

    sent = []

    async def artifacts(_db, _lid):
        return "owen-call-7", None, None

    async def campaign(_db, _dialed):
        return ""

    async def enqueue(facts):
        sent.append(facts)
        return True

    with Patch((runtime, "SessionLocal", FakeDb), (crm_push, "call_artifacts", artifacts),
               (runtime, "_campaign_name_for", campaign),
               (crm_push, "enqueue_call_event", enqueue)):
        for direction in ("outbound", "inbound"):
            asyncio.run(runtime._report_agent_call_to_crm(
                lid="agentoutabc", dialed=DID, caller_number=BOSS, agent_name=AGENT,
                version=4, port="end_call", direction=direction,
                data={"ai_call_extra": {"engine": "retell", "retell_call_id": "call_1",
                                        "direction": direction}}))
    out, inb = sent
    body = to_crm_event(out)
    check("an OUTBOUND CALL row", body["type"] == "CALL" and body["direction"] == "OUTBOUND")
    check("filed under the person called, from our line",
          body["from_number"] == BOSS and body["source_number"] == DID)
    check("completed, with the ai_call and the dedupe key",
          body["call_status"] == "completed" and body["ai_call"]["agent"] == AGENT
          and body["dedupe_key"] == "owen:call:owen-call-7:ended")
    check("the default is still inbound", to_crm_event(inb)["direction"] == "INBOUND")


# --- 6. no answer / busy -------------------------------------------------------------------


def test_no_answer_registers_nothing_and_reports_the_status():
    print("nobody answers, or the line is busy:")
    from app.integrations.crm.events import to_crm_event

    for port, status in (("noanswer", "no-answer"), ("busy", "busy"), ("failed", "failed")):
        ari = OutAri(dial=port, dial_info={"dial_end_cause": 19})
        payload = _payload()
        outcome, bodies, reports, facts, _reg = _run_call(ari, payload)
        check(f"{port}: NO Retell registration (no cost)", bodies == [])
        check(f"{port}: no SIP leg, no bridge", ari.names() == ["originate_pstn", "hangup"])
        check(f"{port}: the outcome is the dial's", outcome == port)
        check(f"{port}: the runtime never ran", reports == [])
        check(f"{port}: ONE CRM event", len(facts) == 1)
        f = facts[0]
        body = to_crm_event(f)
        check(f"{port}: an OUTBOUND call, status {status}",
              body["type"] == "CALL" and body["direction"] == "OUTBOUND"
              and body["call_status"] == status)
        check(f"{port}: on the called number's thread, from our line",
              body["from_number"] == BOSS and body["source_number"] == DID)
        check(f"{port}: who would have called and why",
              body["ai_call"] == {"agent": AGENT, "version": 4, "outcome": port,
                                  "direction": "outbound",
                                  "reason": "your roof inspection on Friday"})
        check(f"{port}: idempotent on the call",
              body["dedupe_key"] == "owen:call:owen-call-7:ended")
        check(f"{port}: the ringing leg is hung up",
              ("hangup", payload["linkedid"]) in ari.ops)


def test_a_take_over_leaves_the_persons_leg_alone():
    print("a supervisor took the call over: the person's leg is theirs:")
    from app.telephony import ownership

    payload = _payload()
    lid = payload["linkedid"]

    async def take_over(ari):
        from app.flows import dtmf

        await asyncio.sleep(0.02)
        ownership.claim(lid, "owen", channels=[lid, "op-chan"])
        # monitor.take_over hangs the Retell leg up; that is what ends the agent's wait.
        dtmf.push_channel_event(ari.out_id, leg_gone(ari.out_id))

    ari = OutAri(script=take_over)
    try:
        outcome, *_ = _run_call(ari, payload)
    finally:
        ownership.clear()
    check("taken_over", outcome == "taken_over")
    check("the person's leg is NOT hung up", ("hangup", lid) not in ari.ops)


# --- 7. inbound unchanged, the new leg is known, status, nothing automatic -----------------


def test_inbound_unchanged_and_the_leg_is_known():
    print("inbound unchanged; the consumer and the projection know the new leg:")
    from app.integrations.retell import brief, client
    from app.providers.asterisk import (AsteriskAdapter, is_agent_outbound_leg,
                                        is_flow_dial_leg, is_outbound_leg)

    body = client.register_body(agent_id="a", from_number=BOSS, to_number=DID, metadata={},
                                variables={})
    check("register_body still defaults to inbound", body["direction"] == "inbound")
    v = brief.render_variables({"known": False}, caller_number=BOSS, dialed_number=DID)
    check("inbound variables are still exactly seven, no call_direction",
          len(v) == 7 and "call_direction" not in v and "call_reason" not in v)
    check("the inbound greeting is unchanged", v["greeting"] == brief.GREETING_UNKNOWN)
    before = dict(v)
    out = brief.for_outbound(v, reason="  the gutter quote.  ", first_name="")
    check("for_outbound does not touch what it is given", v == before)
    check("the reason loses its trailing full stop (the greeting adds one)",
          out["call_reason"] == "the gutter quote"
          and out["greeting"].endswith("I'm calling about the gutter quote."))

    lid = "agentout" + "a" * 32
    start = {"type": "StasisStart", "args": ["agent-outbound"],
             "channel": {"id": lid, "caller": {"number": DID}, "dialplan": {"exten": "s"},
                         "state": "Up",
                         "channelvars": {"CHANNEL(linkedid)": lid,
                                         "X_OWEN_DIRECTION": "outbound",
                                         "X_OWEN_FROM": DID, "X_OWEN_TO": BOSS}}}
    check("the leg is recognised by its appArgs", is_agent_outbound_leg(start))
    check("...and by its id alone (events without args)",
          is_agent_outbound_leg({"type": "ChannelDestroyed", "channel": {"id": lid}}))
    check("it is NOT a flow-dial leg (it IS the call, so it is ingested)",
          not is_flow_dial_leg(start))
    check("it reads as outbound", is_outbound_leg(start))
    evt = AsteriskAdapter().parse_status_event(start)
    check("the projection: outbound, from our DID, TO THE PERSON (X_OWEN_TO)",
          (evt.direction, evt.from_number, evt.to_number) == ("outbound", DID, BOSS))
    op_leg = {"type": "StasisStart", "channel": {
        "id": "op1", "caller": {"number": BOSS}, "dialplan": {"exten": "s"},
        "channelvars": {"CHANNEL(linkedid)": "op1", "X_OWEN_DIRECTION": "outbound",
                        "X_OWEN_FROM": DID}}}
    evt = AsteriskAdapter().parse_status_event(op_leg)
    check("an operator's outbound call is filed exactly as before",
          (evt.from_number, evt.to_number) == (DID, BOSS))
    inbound = {"type": "StasisStart", "channel": {"id": "1.2", "caller": {"number": BOSS},
                                                  "dialplan": {"exten": DID}}}
    check("an inbound call is not an agent-outbound leg", not is_agent_outbound_leg(inbound))

    src = Path(__file__).resolve().parents[1] / "app" / "workers" / "asterisk_consumer.py"
    check("the consumer never starts a flow on it",
          "not is_agent_outbound_leg(event)" in src.read_text(encoding="utf-8"))
    conf = Path(__file__).resolve().parents[2] / "asterisk" / "ari.conf"
    check("ari.conf surfaces X_OWEN_TO",
          "X_OWEN_TO" in [p.strip() for p in conf.read_text(encoding="utf-8")
                          .split("channelvars =", 1)[1].splitlines()[0].split(",")])


def test_status_and_nothing_queues_it_but_a_person():
    print("the CRM can read the status; nothing queues a call by itself:")
    from app.agents import outbound_call

    store = MemoryStore(log=[{"at": datetime.now(timezone.utc).isoformat(), "to": BOSS}])
    with Settings(AGENT_OUTBOUND_ENABLED=False, AGENT_OUTBOUND_FROM=DID,
                  AGENT_OUTBOUND_DAILY_MAX=10):
        s = asyncio.run(outbound_call.status(store))
    check("status: enabled, from_number, daily_max, used_today, hours",
          s == {"enabled": False, "from_number": DID, "daily_max": 10, "used_today": 1,
                "hours": "08:00-20:00 America/New_York"})

    from app.workers.handlers import HANDLERS

    check("the worker knows the job", "agent_outbound_call" in HANDLERS)
    app_dir = Path(__file__).resolve().parents[1] / "app"
    users = sorted(str(p.relative_to(app_dir)).replace("\\", "/")
                   for p in app_dir.rglob("*.py")
                   if "agent_outbound_call" in p.read_text(encoding="utf-8")
                   or "outbound_call.place" in p.read_text(encoding="utf-8"))
    check(f"only place(), the worker and the two doors name it ({users})",
          users == ["agents/outbound_call.py", "integrations/crm/api.py",
                    "workers/handlers.py"])


if __name__ == "__main__":
    test_off_by_default_and_nothing_happens()
    test_every_guard_refuses_and_does_nothing()
    test_the_from_number_and_a_queued_call()
    test_an_answered_call_runs_the_agent_outbound()
    test_the_runtime_reports_an_outbound_call_to_the_crm()
    test_no_answer_registers_nothing_and_reports_the_status()
    test_a_take_over_leaves_the_persons_leg_alone()
    test_inbound_unchanged_and_the_leg_is_known()
    test_status_and_nothing_queues_it_but_a_person()
    retell_guard.assert_untouched()
    print("\nALL AGENT OUTBOUND CALL CHECKS PASSED")

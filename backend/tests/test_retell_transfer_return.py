"""A transfer nobody answers gives the caller back to the SAME Retell agent (owner, 2026-10-08).

The office number is a Quo line whose own no-answer rule forwards after 15 s to another AI
number, so the platform's 25 s ring handed the caller to the wrong agent. Two changes, asserted
by behaviour through `flows/runtime.run_agent_on_call` with the REAL Retell engine and the REAL
function handler, against a mocked Retell, a fake ARI and an in-memory registry:

  1. **ring_seconds** — a `number` / `operator` transfer target may say how long it rings
     (5..60 whole seconds). The dial uses it; without it, OPERATOR_RING_TIMEOUT_SECONDS.
     Validation refuses anything else with a sentence.
  2. **Return to the agent once** — unanswered (noanswer / busy / failed): a SECOND Retell
     registration for the same agent and pinned version, the same brief (not fetched again)
     with `transfer_failed` "yes" and the sorry greeting, recorded as `-agent-2`; in it the
     `transfer` function is refused ("take a message") and every other function works; its
     ending is the node's port; the result says `transfer: {target, outcome,
     returned_to_agent}` for the CRM. If the second session cannot start: today's `transfer`
     port. Answered transfers and owen_voice are unchanged.

Nothing here opens a socket or a database; `tests/retell_guard.py` fails the module if
anything tried to reach Retell.

Run: python -m tests.test_retell_transfer_return
"""

import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx

from tests import retell_guard
from tests.retell_support import (CALLER, CHANNEL, DID, KEY, LINKEDID, FakeAri, MemoryRegistry,
                                  Patch, Settings, leg_gone)
from tests.retell_support import check as _check


def check(name, cond):
    _check(name, cond, "retell_transfer_return")


OFFICE = "+19549147244"
CONFIG = {"engine": "retell", "retell_agent_id": "agent_7f3a", "retell_agent_version": 3,
          "tools": {"transfer": True, "end_call": True, "capture_lead": True},
          "transfer_targets": {"office": {"kind": "number", "target": OFFICE,
                                          "ring_seconds": 12}}}
KNOWN = {"known": True, "source": "crm",
         "contact": {"first_name": "Maria", "last_name": "Ruiz"}}


class DialAri(FakeAri):
    """FakeAri plus the transfer dial: records the ring time, answers with `dial`."""

    def __init__(self, dial="noanswer", **kw):
        super().__init__(**kw)
        self.dial = dial
        self.dials: list[tuple] = []

    async def dial_number(self, channel_id, number, *, caller_id, timeout_s, **_kw):
        self.ops.append(("dial_number", number))
        self.dials.append((number, timeout_s))
        return self.dial

    async def dial_operator(self, channel_id, operators, *, caller_id, timeout_s, **_kw):
        self.ops.append(("dial_operator", tuple(operators)))
        self.dials.append((tuple(operators), timeout_s))
        return self.dial


class Retell:
    """api.retellai.com: hands out call_1, call_2, ...; `fail_from` makes the Nth and later
    registrations a 500."""

    def __init__(self, fail_from=None):
        self.bodies: list[dict] = []
        self.fail_from = fail_from

        def handler(request: httpx.Request):
            self.bodies.append(json.loads(request.content))
            n = len(self.bodies)
            if self.fail_from is not None and n >= self.fail_from:
                return httpx.Response(500, json={"error": "down"})
            return httpx.Response(200, json={"call_id": f"call_{n}"})

        self.transport = httpx.MockTransport(handler)


def _session_script(reg, said: dict):
    """Plays the world once each session is bridged. Session 1: the agent asks to transfer
    to the office. Session 2: it tries to transfer again (refused), captures the message,
    then hangs up (Retell's native end-call)."""
    from app.flows import dtmf
    from app.integrations.retell import functions

    seen = {"n": 0}

    async def script(ari):
        seen["n"] += 1
        n = seen["n"]
        call = {"call_id": f"call_{n}"}
        await asyncio.sleep(0.02)
        if n == 1:
            said["transfer_1"] = await functions.handle(
                "transfer", {"call": call, "args": {"target": "office"}}, reg)
            return
        said["transfer_2"] = await functions.handle(
            "transfer", {"call": call, "args": {"target": "office"}}, reg)
        said["capture_2"] = await functions.handle(
            "capture_lead", {"call": call, "args": {"name": "Maria", "notes": "leak in den"}},
            reg)
        await asyncio.sleep(0.02)
        dtmf.push_channel_event(ari.out_id, leg_gone(ari.out_id))
    return script


class FakeDb:
    def __init__(self):
        self.agent = SimpleNamespace(name="Receptionist")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        return None

    async def get(self, _model, _id):
        return self.agent


def _run(ari, retell, reg, *, session=None, brief=KNOWN):
    """run_agent_on_call with the database, the CRM report and the spend cap faked.
    Returns (AgentRun, what was stored per session, the reports, brief fetches)."""
    from app.agents import retell as engine
    from app.agents import spend
    from app.agents.retell import RetellVoiceAgentSession
    from app.flows import runtime
    from app.integrations.retell import client, functions, registry

    version = SimpleNamespace(id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
                              agent_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
                              config=CONFIG, version=4)
    stored, reports, fetched = [], [], []

    async def resolve(_db, _agent_id):
        return version

    async def pin(*_a):
        return None

    async def store(_pid, _lid, _vid, data):
        stored.append(dict(data))

    async def enqueue_report(*_a, **_kw):
        return None

    async def report_crm(**kw):
        reports.append(kw)

    async def cap():
        return False

    async def no_alert():
        return None

    async def fetch_brief(_spec, _ctx):
        fetched.append(1)
        return brief

    async def version_config(_vid):
        return dict(CONFIG)

    previous = registry.use(reg)
    try:
        with Settings(RETELL_API_KEY=KEY, RETELL_SIP_ENDPOINT="retell",
                      RETELL_SIP_HOST="sip.retellai.com", RETELL_CONNECT_TIMEOUT_SECONDS=2,
                      OPERATOR_RING_TIMEOUT_SECONDS=25), \
                Patch((client, "TRANSPORT", retell.transport), (spend, "over_cap", cap),
                      (spend, "check_alert", no_alert), (engine, "_fetch_brief", fetch_brief),
                      (engine, "POLL_SECONDS", 0.01), (engine, "EXIT_GRACE_SECONDS", 0.0),
                      (functions, "_version_config", version_config),
                      (runtime, "SessionLocal", FakeDb),
                      (runtime, "_resolve_active_agent_version", resolve),
                      (runtime, "_pin_agent_version", pin),
                      (runtime, "_store_agent_output", store),
                      (runtime, "_enqueue_crm_report", enqueue_report),
                      (runtime, "_report_agent_call_to_crm", report_crm),
                      (runtime, "get_session_for_agent",
                       lambda _spec: session or RetellVoiceAgentSession())):
            run = asyncio.run(runtime.run_agent_on_call(
                ari=ari, channel_id=CHANNEL, lid=LINKEDID, dialed=DID, caller_number=CALLER,
                provider_id=1, agent_id=str(version.agent_id)))
    finally:
        registry.use(previous)
    return run, stored, reports, fetched


# --- 1. ring_seconds ----------------------------------------------------------------------


def test_ring_seconds_is_validated():
    print("ring_seconds is validated with a sentence:")
    from app.agents.service import validate_agent_config

    def errors_for(entry):
        cfg = dict(CONFIG, transfer_targets={"office": entry})
        return validate_agent_config(cfg)

    errors, _ = errors_for({"kind": "number", "target": OFFICE, "ring_seconds": 12})
    check("12 seconds on a number target is accepted", errors == [])
    errors, _ = errors_for({"kind": "operator", "target": "desk", "ring_seconds": 5})
    check("5 (the minimum) on an operator target is accepted", errors == [])
    errors, _ = errors_for({"kind": "number", "target": OFFICE, "ring_seconds": 60})
    check("60 (the maximum) is accepted", errors == [])
    for bad in (4, 61, 0, -3):
        errors, _ = errors_for({"kind": "number", "target": OFFICE, "ring_seconds": bad})
        check(f"{bad} is refused, saying the range",
              len(errors) == 1 and "ring_seconds must be from 5 to 60" in errors[0]
              and "'office'" in errors[0])
    for bad in ("12", 12.5, True, None, [12]):
        errors, _ = errors_for({"kind": "number", "target": OFFICE, "ring_seconds": bad})
        check(f"{bad!r} is refused as not a whole number of seconds",
              len(errors) == 1 and "whole number of seconds" in errors[0])
    errors, warnings = validate_agent_config(dict(
        CONFIG, engine="owen_voice", persona="p", greeting="g",
        tools={"transfer": True},
        transfer_targets={"spanish": {"kind": "flow", "target": DID, "ring_seconds": 10}}))
    check("on a flow target it is not an error...",
          not any("ring_seconds" in e for e in errors))
    check("...but a warning that it is ignored", any("ring_seconds is ignored" in w
                                                    for w in warnings))


def test_ring_seconds_reaches_the_dial():
    print("the dial rings for the target's ring_seconds, else the platform default:")
    from app.flows import runtime
    from app.flows.transfer import resolve_transfer_target

    chosen = resolve_transfer_target(CONFIG["transfer_targets"], "office")
    check("the allowlist carries ring_seconds", chosen.get("ring_seconds") == 12)
    plain = resolve_transfer_target({"office": {"kind": "number", "target": OFFICE}}, "office")
    check("absent stays absent", "ring_seconds" not in plain)
    bad = resolve_transfer_target(
        {"office": {"kind": "number", "target": OFFICE, "ring_seconds": 600}}, "office")
    check("an invalid one that slipped past validation is dropped", "ring_seconds" not in bad)

    for target, want in ((chosen, 12.0), (plain, 25.0), (bad, 25.0)):
        for kind in ("number", "operator"):
            ari = DialAri(dial="noanswer")
            with Settings(OPERATOR_RING_TIMEOUT_SECONDS=25):
                asyncio.run(runtime._do_agent_transfer(ari, CHANNEL, LINKEDID,
                                                       dict(target, kind=kind)))
            check(f"{kind}: rang for {want:g} s", ari.dials and ari.dials[0][1] == want)


# --- 2. return to the agent once ---------------------------------------------------------


def test_unanswered_transfer_returns_to_the_same_agent():
    print("an unanswered transfer gives the caller back to the same agent, once:")
    from app.integrations.retell import brief as brief_mod
    from app.integrations.retell import functions

    for outcome in ("noanswer", "busy", "failed"):
        reg, retell, said = MemoryRegistry(), Retell(), {}
        ari = DialAri(dial=outcome, script=_session_script(reg, said))
        run, stored, reports, fetched = _run(ari, retell, reg)
        check(f"[{outcome}] the office was rung once, for its 12 s",
              ari.dials == [(OFFICE, 12.0)])
        check(f"[{outcome}] the first transfer was accepted",
              said.get("transfer_1", {}).get("result") == functions.SAY_TRANSFERRING)
        check(f"[{outcome}] TWO Retell registrations", len(retell.bodies) == 2)
        first, second = retell.bodies
        check(f"[{outcome}] the same Retell agent and pinned version both times",
              first["agent_id"] == second["agent_id"] == "agent_7f3a"
              and first.get("agent_version") == second.get("agent_version") == 3)
        v1 = first["retell_llm_dynamic_variables"]
        v2 = second["retell_llm_dynamic_variables"]
        check(f"[{outcome}] session 1: transfer_failed = no, the normal greeting",
              v1["transfer_failed"] == "no" and v1["greeting"].startswith("Hi Maria"))
        check(f"[{outcome}] session 2: transfer_failed = yes",
              v2["transfer_failed"] == "yes")
        check(f"[{outcome}] session 2: the sorry greeting, word for word",
              v2["greeting"] == "Sorry, nobody could pick up right now. I'll make sure the "
                                "office gets your message - what would you like me to pass on?"
              and v2["greeting"] == brief_mod.GREETING_TRANSFER_FAILED)
        check(f"[{outcome}] session 2: the same brief otherwise",
              {k: x for k, x in v2.items() if k not in ("transfer_failed", "greeting")}
              == {k: x for k, x in v1.items() if k not in ("transfer_failed", "greeting")})
        check(f"[{outcome}] the CRM brief was asked ONCE (no second wait for the caller)",
              len(fetched) == 1)
        check(f"[{outcome}] session 2's metadata says so",
              second["metadata"].get("after_failed_transfer") is True
              and "after_failed_transfer" not in first["metadata"])
        check(f"[{outcome}] session 2: transfer refused, told to take a message",
              said.get("transfer_2", {}).get("result") == functions.SAY_TRANSFER_TRIED
              and "message" in functions.SAY_TRANSFER_TRIED)
        check(f"[{outcome}] session 2: no second exit request was written",
              reg.rows["call_2"]["exit_port"] is None)
        check(f"[{outcome}] session 2: other functions still work",
              said.get("capture_2", {}).get("result") == functions.SAY_CAPTURED
              and reg.rows["call_2"]["captured"] == {"name": "Maria", "notes": "leak in den"})
        check(f"[{outcome}] the office was not rung a second time", len(ari.dials) == 1)
        check(f"[{outcome}] the port is session 2's ending (Retell hung up -> end_call)",
              run.port == "end_call")
        recs = [o[2] for o in ari.ops if o[0] == "record_bridge"]
        check(f"[{outcome}] two recordings, -agent-1 then -agent-2",
              recs == [f"{LINKEDID}-agent-1", f"{LINKEDID}-agent-2"])
        check(f"[{outcome}] each session's output was stored as it ended",
              [d.get("recording_name") for d in stored]
              == [f"{LINKEDID}-agent-1", f"{LINKEDID}-agent-2"])
        extra = run.data.get("ai_call_extra") or {}
        check(f"[{outcome}] the result records the transfer for the CRM",
              extra.get("transfer") == {"target": "office", "outcome": outcome,
                                        "returned_to_agent": True})
        check(f"[{outcome}] the capture from session 2 rides the result",
              run.data.get("captured") == {"name": "Maria", "notes": "leak in den"})
        check(f"[{outcome}] the CRM report carries the final port and the transfer record",
              len(reports) == 1 and reports[0]["port"] == "end_call"
              and reports[0]["data"]["ai_call_extra"]["transfer"]["returned_to_agent"] is True)
        check(f"[{outcome}] 'transfer' (where it went) is NOT claimed",
              "transfer" not in run.data)
    retell_guard.assert_untouched()


def test_the_caller_hanging_up_in_session_2_is_default():
    print("the caller hanging up during the second session -> default:")
    from app.flows import dtmf
    from app.integrations.retell import functions

    reg, retell = MemoryRegistry(), Retell()
    seen = {"n": 0}

    async def script(ari):
        seen["n"] += 1
        await asyncio.sleep(0.02)
        if seen["n"] == 1:
            await functions.handle("transfer", {"call": {"call_id": "call_1"},
                                                "args": {"target": "office"}}, reg)
        else:
            dtmf.push_channel_event(CHANNEL, leg_gone(CHANNEL))

    run, _s, _r, _f = _run(DialAri(dial="noanswer", script=script), retell, reg)
    check("default", run.port == "default")
    check("...still recorded as returned to the agent",
          run.data["ai_call_extra"]["transfer"]["returned_to_agent"] is True)


def test_answered_transfer_is_unchanged():
    print("an ANSWERED transfer is unchanged: transferred, one registration, caller hung up:")
    reg, retell, said = MemoryRegistry(), Retell(), {}
    ari = DialAri(dial="answered", script=_session_script(reg, said))
    run, _stored, reports, _f = _run(ari, retell, reg)
    check("transferred", run.port == "transferred")
    check("one Retell registration only", len(retell.bodies) == 1)
    check("rung for the target's 12 s", ari.dials == [(OFFICE, 12.0)])
    check("the transfer names where the caller went",
          run.data.get("transfer", {}).get("name") == "office")
    check("no return-to-agent record", "transfer" not in (run.data.get("ai_call_extra") or {}))
    check("the caller's leg is hung up when the conversation ends",
          ("hangup", CHANNEL) in ari.ops)
    check("reported as transferred", reports and reports[0]["port"] == "transferred")


def test_second_session_that_cannot_start_is_todays_transfer_port():
    print("the second session cannot start -> today's transfer port (voicemail):")
    reg, retell, said = MemoryRegistry(), Retell(fail_from=2), {}
    ari = DialAri(dial="noanswer", script=_session_script(reg, said))
    run, stored, reports, _f = _run(ari, retell, reg)
    check("two registrations were tried", len(retell.bodies) == 2)
    check("the second failed, so the port is transfer", run.port == "transfer")
    check("the Retell leg was dialled once only",
          sum(1 for o in ari.ops if o[0] == "originate") == 1)
    check("recorded: not returned to the agent",
          run.data["ai_call_extra"]["transfer"] == {"target": "office", "outcome": "noanswer",
                                                    "returned_to_agent": False})
    check("reported with the transfer port", reports and reports[0]["port"] == "transfer")

    print("...and so is the spend cap reached before the second session:")
    from app.agents import retell as engine
    from app.agents import spend

    reg, retell, said = MemoryRegistry(), Retell(), {}
    calls = {"n": 0}

    async def cap_second():
        calls["n"] += 1
        return calls["n"] >= 2

    # _run patches over_cap with "never"; this wraps the engine so the SECOND session sees
    # the cap reached, through the engine's own check.
    original = engine.RetellVoiceAgentSession.run

    async def run_capped(self, spec, ctx):
        with Patch((spend, "over_cap", cap_second)):
            return await original(self, spec, ctx)

    with Patch((engine.RetellVoiceAgentSession, "run", run_capped)):
        ari = DialAri(dial="noanswer", script=_session_script(reg, said))
        run, _s, _r, _f = _run(ari, retell, reg)
    check("spend cap on the second session -> transfer port", run.port == "transfer")
    check("one registration only", len(retell.bodies) == 1)

    print("...and a caller who hung up while it rang gets no second session:")
    reg, retell, said = MemoryRegistry(), Retell(), {}
    ari = DialAri(dial="failed", script=_session_script(reg, said))
    ari._last_dial = {"dial_ended_by": "caller"}
    run, _s, _r, _f = _run(ari, retell, reg)
    check("transfer port, one registration", run.port == "transfer" and len(retell.bodies) == 1)
    check("recorded as the caller leaving",
          run.data["ai_call_extra"]["transfer"]["outcome"] == "caller_gone")


def test_owen_voice_is_unchanged():
    print("owen_voice: an unanswered transfer still takes the transfer port, no second run:")
    from app.agents.session import AgentResult

    class OwenVoice:
        name = "owen_voice"

        def __init__(self):
            self.runs = 0

        async def run(self, spec, ctx):
            self.runs += 1
            return AgentResult(port="transfer", data={"destination": "office"})

    for dial, want in (("noanswer", "transfer"), ("busy", "transfer"),
                       ("answered", "transferred")):
        session, reg, retell = OwenVoice(), MemoryRegistry(), Retell()
        ari = DialAri(dial=dial)
        run, _s, _r, _f = _run(ari, retell, reg, session=session)
        check(f"[{dial}] port {want}", run.port == want)
        check(f"[{dial}] the session ran once", session.runs == 1)
        check(f"[{dial}] nothing reached Retell", retell.bodies == [])
        check(f"[{dial}] no return-to-agent record",
              "ai_call_extra" not in run.data)
        check(f"[{dial}] rang for the target's ring_seconds", ari.dials == [(OFFICE, 12.0)])


if __name__ == "__main__":
    test_ring_seconds_is_validated()
    test_ring_seconds_reaches_the_dial()
    test_unanswered_transfer_returns_to_the_same_agent()
    test_the_caller_hanging_up_in_session_2_is_default()
    test_answered_transfer_is_unchanged()
    test_second_session_that_cannot_start_is_todays_transfer_port()
    test_owen_voice_is_unchanged()
    retell_guard.assert_untouched()
    print("\nALL RETELL TRANSFER-RETURN CHECKS PASSED")

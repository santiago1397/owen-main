"""The `retell` engine: register with Retell, dial it over SIP, bridge, wait — or voicemail.

Asserted by behaviour against a mocked Retell (an `httpx.MockTransport` — the real request
code runs), a fake ARI and an in-memory registry. Nothing here opens a socket or a database,
and `tests/retell_guard.py` fails the module if anything tried to reach Retell.

  1. **Success.** One registration with the C2 dynamic variables (the brief STARTS with the
     disclosure rule), no `agent_version` unless pinned, the key as a Bearer token; the SIP
     leg dialled as `PJSIP/<endpoint>/sip:<call_id>@<host>`; caller + Retell bridged and the
     BRIDGE recorded as `<linkedid>-agent-1`; the registry row ends `ended` with the
     channels; the result carries what the runtime and the CRM report read.
  2. **Off unless configured.** No key -> `failed`, ZERO requests, no ARI operation, nothing
     registered. Same for no `retell_agent_id`, and for the spend cap.
  3. **Every failure is `failed`** (-> the flow's fallback, voicemail): 4xx, 5xx, timeout,
     an answer without `call_id`, the call id not recordable, the leg not answering, the
     bridge refused.
  4. **How it ends** decides the port: Retell hangs up -> end_call; the caller hangs up ->
     default; a function asked to transfer -> transfer with the destination NAME; a
     supervisor took over -> taken_over, and then NOTHING here touches the call.
  5. **C1 validation**: engine "retell" needs retell_agent_id (<= 100), tools within
     {transfer, end_call, capture_lead, request_change}, never send_sms; persona, greeting,
     voice and knowledge not required and the 6,000-character rule not applied.

Run: python -m tests.test_retell_engine
"""

import asyncio

import httpx

from tests import retell_guard
from tests.retell_support import (CALLER, CHANNEL, DID, KEY, LINKEDID, FakeAri,
                                  MemoryRegistry, MockRetell, Patch, Settings, leg_gone)
from tests.retell_support import check as _check


def check(name, cond):
    _check(name, cond, "retell_engine")


CONFIG = {"engine": "retell", "retell_agent_id": "agent_7f3a", "tools": {"transfer": True},
          "transfer_targets": {"office": {"kind": "operator", "target": "desk"}}}


def _spec(**config):
    from app.agents.session import AgentSpec

    cfg = dict(CONFIG)
    cfg.update(config)
    spec = AgentSpec(agent_id="ag-1", version_id="11111111-1111-1111-1111-111111111111",
                     engine="retell", config=cfg, tools=cfg.get("tools") or {},
                     guardrails=cfg.get("guardrails") or {})
    spec.agent_name = "Receptionist"
    spec.version_number = 4
    return spec


def _ctx(ari):
    from app.agents.session import AgentCallContext

    return AgentCallContext(channel_id=CHANNEL, linkedid=LINKEDID, caller_number=CALLER,
                            ari=ari, dialed_number=DID)


def _run(spec, ari, retell: MockRetell, reg: MemoryRegistry, *, key=KEY, over_cap=False,
         brief=None):
    """Run the engine once with everything faked; return (result, reg)."""
    from app.agents import retell as engine
    from app.agents import spend
    from app.integrations.retell import client, registry

    async def cap():
        return over_cap

    async def no_alert():
        return None

    async def fetch_brief(_spec, _ctx):
        return brief

    previous = registry.use(reg)
    try:
        with Settings(RETELL_API_KEY=key, RETELL_SIP_ENDPOINT="retell",
                      RETELL_SIP_HOST="sip.retellai.com", RETELL_CONNECT_TIMEOUT_SECONDS=2), \
                Patch((client, "TRANSPORT", retell.transport), (spend, "over_cap", cap),
                      (spend, "check_alert", no_alert), (engine, "_fetch_brief", fetch_brief),
                      (engine, "POLL_SECONDS", 0.01), (engine, "EXIT_GRACE_SECONDS", 0.0)):
            result = asyncio.run(engine.RetellVoiceAgentSession().run(spec, _ctx(ari)))
    finally:
        registry.use(previous)
    return result


def _retell_hangs_up(after=0.03):
    async def script(ari):
        from app.flows import dtmf

        await asyncio.sleep(after)
        dtmf.push_channel_event(ari.out_id, leg_gone(ari.out_id))
    return script


# --- 1. success --------------------------------------------------------------------------


def test_success_registers_dials_bridges_records_and_ends():
    print("a call Retell answers and later ends:")
    retell, reg = MockRetell(), MemoryRegistry()
    ari = FakeAri(script=_retell_hangs_up())
    known = {"known": True, "source": "crm", "contact": {"first_name": "Maria",
                                                         "last_name": "Ruiz"},
             "address": "12 Palm Ave, Bradenton"}
    result = _run(_spec(), ari, retell, reg, brief=known)

    check("exactly one request to Retell", len(retell.requests) == 1)
    req = retell.requests[0]
    check("POST /v2/register-phone-call",
          req.method == "POST" and req.url.path == "/v2/register-phone-call")
    check("authorised with the key as a Bearer token",
          req.headers.get("authorization") == f"Bearer {KEY}")
    body = retell.sent_json()
    check("for the agent's Retell id", body["agent_id"] == "agent_7f3a")
    check("from the caller, to the dialled DID, inbound",
          (body["from_number"], body["to_number"], body["direction"]) == (CALLER, DID, "inbound"))
    check("metadata names the linkedid, the OWEN agent and its version",
          body["metadata"] == {"linkedid": LINKEDID, "owen_agent": "Receptionist",
                               "owen_version": 4})
    check("no agent_version: Retell picks it (decision 14)", "agent_version" not in body)
    v = body["retell_llm_dynamic_variables"]
    check("the C2 variables plus the greeting and transfer_failed, all strings",
          set(v) == {"customer_known", "customer_first_name", "customer_brief",
                     "caller_number", "dialed_number", "greeting", "transfer_failed"}
          and all(isinstance(x, str) for x in v.values()))
    check("a normal call: transfer_failed = no", v["transfer_failed"] == "no")
    check("known caller, first name given", v["customer_known"] == "yes"
          and v["customer_first_name"] == "Maria")
    from app.integrations.retell.brief import DISCLOSURE_RULE

    check("the brief STARTS with the disclosure rule", v["customer_brief"].startswith(DISCLOSURE_RULE))
    check("the dialled number reached Retell (the old bug: never carried)",
          v["dialed_number"] == DID and v["caller_number"] == CALLER)

    check("the SIP leg is the call id through the PJSIP endpoint",
          ari.endpoint == "PJSIP/retell/sip:call_abc123@sip.retellai.com")
    check("the leg id carries the flow-dial marker (never a new inbound call)",
          ari.out_id.startswith("flow-dial-retell-"))
    adds = [o for o in ari.ops if o[0] == "add_to_bridge"]
    check("the caller and the Retell leg are bridged together",
          adds and set(adds[0][2]) == {CHANNEL, ari.out_id})
    check("the BRIDGE is recorded, under the agent recording name",
          ("record_bridge", "bridge-1", f"{LINKEDID}-agent-1") in ari.ops)
    check("Retell hung up -> end_call", result.port == "end_call")
    check("the bridge is torn down after", ("destroy_bridge", "bridge-1") in ari.ops)
    check("the caller's channel is never hung up by the engine",
          ("hangup", CHANNEL) not in ari.ops)

    row = reg.rows["call_abc123"]
    check("the call id was persisted with its linkedid (webhooks after a restart)",
          row["linkedid"] == LINKEDID and row["retell_agent_id"] == "agent_7f3a")
    check("with the channels and bridge take-over needs",
          row["call_channel_id"] == CHANNEL and row["retell_channel_id"] == ari.out_id
          and row["bridge_id"] == "bridge-1")
    check("and it ends 'ended'", row["status"] == "ended")
    d = result.data
    check("the result names the engine and the Retell call",
          d["engine"] == "retell" and d["retell_call_id"] == "call_abc123")
    check("the recording name the runtime registers", d["recording_name"] == f"{LINKEDID}-agent-1")
    check("C4's engine fields for the CRM report",
          d["ai_call_extra"] == {"engine": "retell", "retell_call_id": "call_abc123"})


def test_an_unknown_caller_still_gets_the_rule_and_nothing_else():
    print("an unknown caller: the rule, and 'no record' — never somebody else's facts:")
    retell = MockRetell()
    _run(_spec(), FakeAri(script=_retell_hangs_up()), retell, MemoryRegistry(),
         brief={"known": False})
    v = retell.sent_json()["retell_llm_dynamic_variables"]
    from app.integrations.retell.brief import DISCLOSURE_RULE, UNKNOWN_LINE

    check("customer_known is no, no first name",
          v["customer_known"] == "no" and v["customer_first_name"] == "")
    check("the brief is the rule then 'no record'",
          v["customer_brief"] == f"{DISCLOSURE_RULE}\n\n{UNKNOWN_LINE}")


def test_a_pinned_retell_version_is_sent():
    print("an agent version that pins retell_agent_version sends it:")
    retell = MockRetell()
    _run(_spec(retell_agent_version=3), FakeAri(script=_retell_hangs_up()), retell,
         MemoryRegistry())
    check("agent_version is the pinned integer", retell.sent_json()["agent_version"] == 3)


# --- 2. off unless configured ------------------------------------------------------------


def test_no_key_means_failed_and_no_request_at_all():
    print("no RETELL_API_KEY: failed, zero requests, nothing touched:")
    retell, reg, ari = MockRetell(), MemoryRegistry(), FakeAri()
    result = _run(_spec(), ari, retell, reg, key="")
    check("the failed port (-> voicemail)", result.port == "failed")
    check("ZERO requests to Retell", retell.requests == [])
    check("no ARI operation", ari.ops == [])
    check("nothing registered", reg.rows == {} and reg.calls == [])

    print("no retell_agent_id: the same:")
    retell, ari = MockRetell(), FakeAri()
    result = _run(_spec(retell_agent_id=""), ari, retell, MemoryRegistry())
    check("failed, no request, no ARI", result.port == "failed" and retell.requests == []
          and ari.ops == [])

    print("over the spend cap: the same:")
    retell, ari = MockRetell(), FakeAri()
    result = _run(_spec(), ari, retell, MemoryRegistry(), over_cap=True)
    check("failed, no request, no ARI", result.port == "failed" and retell.requests == []
          and ari.ops == [])


# --- 3. every failure is `failed` --------------------------------------------------------


def test_registration_failures_take_the_failed_port_and_dial_nothing():
    cases = [
        ("a 4xx", MockRetell(status=422, body={"error_message": "bad agent"})),
        ("a 5xx", MockRetell(status=503, body={"error_message": "down"})),
        ("a timeout", MockRetell(raise_exc=httpx.ReadTimeout("slow"))),
        ("a connection error", MockRetell(raise_exc=httpx.ConnectError("refused"))),
        ("an answer with no call_id", MockRetell(body={"agent_id": "agent_7f3a"})),
    ]
    for label, retell in cases:
        print(f"registration fails with {label}:")
        ari, reg = FakeAri(), MemoryRegistry()
        result = _run(_spec(), ari, retell, reg)
        check("failed", result.port == "failed")
        check("Retell was asked exactly once (no retry while a caller waits)",
              len(retell.requests) == 1)
        check("no SIP leg dialled, nothing registered", ari.ops == [] and reg.rows == {})


def test_failures_after_registration_take_the_failed_port():
    print("the call id cannot be recorded here:")
    ari = FakeAri()
    result = _run(_spec(), ari, MockRetell(), MemoryRegistry(fail_create=True))
    check("failed, and no leg dialled (no webhook could find it)",
          result.port == "failed" and ari.ops == [])

    print("the Retell SIP leg does not answer:")
    ari, reg = FakeAri(answer="noanswer"), MemoryRegistry()
    result = _run(_spec(), ari, MockRetell(), reg)
    check("failed", result.port == "failed")
    check("the leg is hung up anyway (our timer may have beaten ARI's)",
          ("hangup", ari.out_id) in ari.ops)
    check("no bridge was made", "create_bridge" not in ari.names())
    check("the row is ended", reg.rows["call_abc123"]["status"] == "ended")

    print("the bridge is refused:")
    ari = FakeAri(bridge_ok=False)
    result = _run(_spec(), ari, MockRetell(), MemoryRegistry())
    check("failed", result.port == "failed")
    check("nothing recorded", "record_bridge" not in ari.names())
    check("the leg hung up and the bridge destroyed",
          ("hangup", ari.out_id) in ari.ops and ("destroy_bridge", "bridge-1") in ari.ops)


# --- 4. how it ends ----------------------------------------------------------------------


def test_the_caller_hanging_up_is_default():
    print("the caller hangs up first:")

    async def script(ari):
        from app.flows import dtmf

        await asyncio.sleep(0.03)
        dtmf.push_channel_event(CHANNEL, leg_gone(CHANNEL))

    ari = FakeAri(script=script)
    result = _run(_spec(), ari, MockRetell(), MemoryRegistry())
    check("default", result.port == "default")
    check("the Retell leg is hung up after", ("hangup", ari.out_id) in ari.ops)


def test_a_transfer_function_ends_the_leg_and_names_the_destination():
    print("a transfer function call ends the agent's part:")
    reg = MemoryRegistry()

    async def script(ari):
        await asyncio.sleep(0.03)
        await reg.request_exit("call_abc123", "transfer", {"destination": "office"})

    ari = FakeAri(script=script)
    result = _run(_spec(), ari, MockRetell(), reg)
    check("the transfer port", result.port == "transfer")
    check("with the destination NAME for the runtime's allowlist",
          result.data.get("destination") == "office")
    check("the Retell leg is hung up so the caller can be moved",
          ("hangup", ari.out_id) in ari.ops and ("destroy_bridge", "bridge-1") in ari.ops)


def test_a_take_over_stands_everything_down():
    print("a supervisor takes over:")
    from app.telephony import ownership

    async def script(ari):
        from app.flows import dtmf

        await asyncio.sleep(0.03)
        ownership.claim(LINKEDID, "owen-desk", channels=[CHANNEL, "op-chan"])
        dtmf.push_channel_event(ari.out_id, leg_gone(ari.out_id))

    ari = FakeAri(script=script)
    try:
        result = _run(_spec(), ari, MockRetell(), MemoryRegistry())
        check("taken_over", result.port == "taken_over")
        check("the bridge the operator is joining is NOT destroyed",
              "destroy_bridge" not in ari.names())
        check("and nothing is hung up by the engine", "hangup" not in ari.names())
    finally:
        ownership.clear()


def test_captures_and_requests_ride_the_result():
    print("a capture and a change request made during the call reach the result:")
    reg = MemoryRegistry()

    async def script(ari):
        from app.flows import dtmf

        await reg.merge_capture("call_abc123", {"name": "Maria", "intent": "leak"})
        await reg.add_request("call_abc123", {"kind": "reschedule", "request": "Friday",
                                              "created": True, "where": "task"})
        await asyncio.sleep(0.02)
        dtmf.push_channel_event(ari.out_id, leg_gone(ari.out_id))

    result = _run(_spec(), FakeAri(script=script), MockRetell(), reg)
    check("captured -> data.captured (stored as call_captures by the runtime)",
          result.data.get("captured") == {"name": "Maria", "intent": "leak"})
    check("requests -> ai_call.requests",
          result.data["ai_call_extra"].get("requests")[0]["kind"] == "reschedule")


# --- 5. C1 validation --------------------------------------------------------------------


def test_c1_validation():
    print("RETELL-PLAN C1 at activation:")
    from app.agents.service import validate_agent_config

    errors, warnings = validate_agent_config(dict(CONFIG, knowledge="x" * 9000))
    check("a minimal Retell agent is valid — no persona, greeting, voice needed", errors == [])
    check("and the 6,000-character knowledge rule does not apply", errors == [])
    check("no persona/greeting nagging for an agent Retell voices",
          not any("persona" in w or "greeting" in w for w in warnings))

    errors, _ = validate_agent_config({"engine": "retell"})
    check("retell_agent_id is required", any("retell_agent_id" in e for e in errors))
    errors, _ = validate_agent_config({"engine": "retell", "retell_agent_id": "a" * 101})
    check("...and at most 100 characters", any("100" in e for e in errors))

    errors, _ = validate_agent_config(dict(CONFIG, tools={"send_sms": True}))
    check("send_sms is refused, by name, with the reason",
          len(errors) == 1 and "send_sms" in errors[0] and "never" in errors[0])
    errors, _ = validate_agent_config(dict(CONFIG, tools={
        "transfer": True, "end_call": True, "capture_lead": True, "request_change": True}))
    check("the four Retell tools are allowed together", errors == [])

    errors, _ = validate_agent_config({"engine": "owen_voice", "tools": {"request_change": True}})
    check("request_change is refused on owen_voice (it has no such tool)",
          any("request_change" in e for e in errors))
    check("the engine is registered", "retell" in __import__(
        "app.agents.session", fromlist=["_ENGINES"])._ENGINES)


if __name__ == "__main__":
    test_success_registers_dials_bridges_records_and_ends()
    test_an_unknown_caller_still_gets_the_rule_and_nothing_else()
    test_a_pinned_retell_version_is_sent()
    test_no_key_means_failed_and_no_request_at_all()
    test_registration_failures_take_the_failed_port_and_dial_nothing()
    test_failures_after_registration_take_the_failed_port()
    test_the_caller_hanging_up_is_default()
    test_a_transfer_function_ends_the_leg_and_names_the_destination()
    test_a_take_over_stands_everything_down()
    test_captures_and_requests_ride_the_result()
    test_c1_validation()
    retell_guard.assert_untouched()
    print("\nALL RETELL ENGINE CHECKS PASSED")

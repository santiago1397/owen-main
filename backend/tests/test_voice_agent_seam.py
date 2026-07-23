"""Unit test for the VoiceAgentSession seam + agent versioning (Ticket 11).

Dependency-free by design (like test_flow_interpreter / test_flow_versioning): exercises the
PURE app.agents seam — registry, kill-switch, the dummy engine, the tool registry, the spec
builder, the activation validator, and the append-only version kernel. It does NOT hit
Postgres/httpx/pydantic-settings (none are in this sandbox — the seam imports settings LAZILY
so it stays importable here; the DB glue in app/flows/runtime.py is a SEPARATE unrun path).

Asserts:
- the registry exposes dummy (live) + openai_realtime/vapi/diy (stubbed, raise on run);
- the kill-switch: forced engine wins, else per-agent engine, else the dummy default;
- the dummy engine returns each valid exit port and simulates capture_lead -> data.captured;
- the tool registry: fixed tools, flow-exit ports, per-agent toggles filter unknowns;
- build_spec flattens a version config; validate_agent_config gates activation;
- agent_versions are append-only (version = max+1; prior rows never mutated).

Run: python -m tests.test_voice_agent_seam
"""

import asyncio
import copy
import sys

from app.agents.service import build_spec, next_version_number, validate_agent_config
from app.agents.session import (
    AgentCallContext,
    AgentResult,
    DummyVoiceAgentSession,
    _select_engine,
    get_voice_agent_session,
    select_voice_agent_engine,
)
from app.agents.tools import FLOW_EXIT_PORTS, TOOLS, VALID_PORTS, enabled_tools, is_valid_port


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"voice_agent_seam failed at: {name}")


CTX = AgentCallContext(channel_id="chan-1", linkedid="169.1")


def _run_dummy(config):
    spec = build_spec("A1", "A1-v1", config)
    session = DummyVoiceAgentSession()
    return asyncio.run(session.run(spec, CTX))


def test_registry_and_stubs():
    print("registry — dummy is live, openai_realtime/vapi/diy are registered-but-stubbed:")
    dummy = get_voice_agent_session("dummy")
    check("dummy engine name", dummy.name == "dummy")
    res = asyncio.run(dummy.run(build_spec("A1", "v1", {"engine": "dummy"}), CTX))
    check("dummy.run returns an AgentResult", isinstance(res, AgentResult))
    for name in ("openai_realtime", "vapi", "diy"):
        eng = get_voice_agent_session(name)
        check(f"{name} constructs (registered)", eng.name == name)
        raised = False
        try:
            asyncio.run(eng.run(build_spec("A1", "v1", {"engine": name}), CTX))
        except NotImplementedError:
            raised = True
        check(f"{name}.run raises NotImplementedError (Ticket 12 fills it in)", raised)
    check("unknown engine falls back to dummy (safe/offline)",
          get_voice_agent_session("nope").name == "dummy")


def test_kill_switch_and_selection():
    print("kill-switch — forced global engine wins, else per-agent, else dummy default:")
    check("forced wins over per-agent", _select_engine("dummy", "openai_realtime") == "dummy")
    check("no force -> per-agent engine", _select_engine("", "openai_realtime") == "openai_realtime")
    check("no force + no per-agent -> dummy", _select_engine("", "") == "dummy")
    check("whitespace force ignored", _select_engine("   ", "vapi") == "vapi")
    # In the sandbox settings import fails -> _forced_engine() == "" -> honours per-agent.
    check("select_voice_agent_engine honours per-agent when unforced",
          select_voice_agent_engine("openai_realtime") == "openai_realtime")
    check("select_voice_agent_engine defaults to dummy", select_voice_agent_engine(None) == "dummy")


def test_dummy_ports_and_capture_lead():
    print("dummy engine — returns each valid exit port + capture_lead surfaces data.captured:")
    for port in ("transfer", "end_call", "default", "failed"):
        res = _run_dummy({"engine": "dummy", "dummy": {"port": port}})
        check(f"dummy returns scripted port '{port}'", res.port == port)
    check("dummy default port is end_call", _run_dummy({"engine": "dummy"}).port == "end_call")
    check("invalid scripted port coerced to end_call",
          _run_dummy({"engine": "dummy", "dummy": {"port": "bogus"}}).port == "end_call")
    # capture_lead toggle -> a captured lead payload flows out via data (the analysis seam).
    off = _run_dummy({"engine": "dummy", "tools": {}})
    check("no capture_lead -> no captured data", "captured" not in off.data)
    on = _run_dummy({"engine": "dummy", "tools": {"capture_lead": True}})
    check("capture_lead toggle -> data.captured present", isinstance(on.data.get("captured"), dict))


def test_tools_registry():
    print("tool registry — fixed set, flow-exit ports, per-agent toggles filter unknowns:")
    check("the four fixed tools exist", set(TOOLS) == {"transfer", "end_call", "capture_lead", "send_sms"})
    check("flow-exit ports are transfer/end_call", FLOW_EXIT_PORTS == frozenset({"transfer", "end_call"}))
    check("valid ports add default/failed", VALID_PORTS == frozenset({"transfer", "end_call", "default", "failed"}))
    check("is_valid_port true for a real port", is_valid_port("transfer"))
    check("is_valid_port false for junk", not is_valid_port("nope"))
    en = enabled_tools({"transfer": True, "send_sms": False, "not_a_tool": True})
    check("only toggled-ON known tools enabled", set(en) == {"transfer"})
    check("None toggles -> nothing enabled", enabled_tools(None) == {})


def test_build_spec_and_validate():
    print("build_spec flattens config; validate_agent_config gates activation:")
    spec = build_spec("A1", "v3", {
        "persona": "helpful", "voice": "alloy", "greeting": "hi", "model": "gpt",
        "engine": "openai_realtime", "tools": {"transfer": True},
        "knowledge": "kb", "guardrails": {"max_call_seconds": 120},
    })
    check("spec carries persona/engine/tools/guardrails",
          spec.persona == "helpful" and spec.engine == "openai_realtime"
          and spec.tools == {"transfer": True} and spec.guardrails["max_call_seconds"] == 120)
    check("missing config -> safe defaults (engine dummy)", build_spec("A1", None, None).engine == "dummy")

    errs, warns = validate_agent_config({"engine": "dummy", "greeting": "hi", "persona": "p"})
    check("clean config -> no errors", errs == [])
    errs2, _ = validate_agent_config({"engine": "nope"})
    check("unknown engine -> error", any("engine" in e for e in errs2))
    errs3, _ = validate_agent_config({"engine": "dummy", "tools": {"ghost": True}})
    check("unknown tool -> error", any("tool" in e for e in errs3))
    _, warns4 = validate_agent_config({"engine": "dummy"})
    check("no greeting/persona -> warnings (non-blocking)", len(warns4) >= 1)


def test_agent_version_append_only():
    print("agent versioning — append-only (version = max+1; prior rows never mutated):")
    check("empty -> 1", next_version_number([]) == 1)
    check("[1,2] -> 3", next_version_number([1, 2]) == 3)
    store: list[dict] = []

    def save(config):
        version = next_version_number([r["version"] for r in store])
        rec = {"version": version, "config": copy.deepcopy(config)}
        store.append(rec)
        return rec

    v1 = save({"persona": "a"})
    v1_snap = copy.deepcopy(v1)
    v2 = save({"persona": "b", "tools": {"transfer": True}})
    check("second save -> version 2", v2["version"] == 2)
    check("saving v2 did NOT mutate v1", store[0] == v1_snap)
    later = {"persona": "c"}
    saved = save(later)
    later["persona"] = "MUTATED"
    check("stored snapshot isolated from later mutation", saved["config"]["persona"] == "c")
    check("all versions retained in order", [r["version"] for r in store] == [1, 2, 3])


def main():
    test_registry_and_stubs()
    test_kill_switch_and_selection()
    test_dummy_ports_and_capture_lead()
    test_tools_registry()
    test_build_spec_and_validate()
    test_agent_version_append_only()
    print("\nALL VOICE AGENT SEAM CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)

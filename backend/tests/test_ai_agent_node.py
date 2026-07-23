"""Unit test for the interpreter's `ai_agent` node driving the VoiceAgentSession seam (Ticket 11).

Dependency-free (like test_flow_interpreter): the PURE FlowInterpreter runs against a FAKE ARI
client and an injected `run_agent` seam that uses the REAL dummy VoiceAgentSession end-to-end
(build_spec -> get_session_for_agent -> session.run). No Postgres/httpx. The actual DB pin
(app/flows/runtime.py `_pin_agent_version`) needs sqlalchemy and is an UNRUN path in the
sandbox; here the pin is proven at the seam by a recorder the fake run_agent writes to.

Asserts:
- the node runs a session and EXITS by the returned port for each of transfer/end_call/default;
- a `failed` port that is unwired falls through to default_fallback (never dead air);
- a run_agent that RAISES routes to `failed` -> fallback (the agent never dead-airs);
- the agent NEVER bridges — no dial/bridge op is issued by the node itself;
- the agent_version is pinned on node entry (recorded once, before routing onward);
- with NO run_agent injected the node keeps its legacy stub (routes to `default`).

Run: python -m tests.test_ai_agent_node
"""

import asyncio
import sys

from app.agents.service import build_spec
from app.agents.session import AgentCallContext, get_session_for_agent
from app.flows.interpreter import FlowInterpreter

# Reuse the fakes from the flow interpreter test (importing the module does not run its main).
from tests.test_flow_interpreter import CHAN, LINKEDID, FakeAri, Recorder


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"ai_agent_node failed at: {name}")


def _graph(want):
    # entry -> ai_agent(agent A1). The node wires transfer/end_call/default; `failed` is left
    # UNWIRED so a failure falls through to default_fallback (vm). `_want` scripts the dummy.
    return {
        "default_fallback": "vm",
        "nodes": {
            "start": {"type": "entry", "next": {"default": "agent"}},
            "agent": {"type": "ai_agent", "agent_id": "A1", "_want": want,
                      "next": {"transfer": "xfer", "end_call": "endc", "default": "dflt"}},
            "xfer": {"type": "hangup"},
            "endc": {"type": "hangup"},
            "dflt": {"type": "hangup"},
            "vm": {"type": "voicemail", "media": "sound:vm", "next": {"default": "bye"}},
            "bye": {"type": "hangup"},
        },
    }


def _make_run_agent(pins, *, raise_it=False):
    """A run_agent seam that pins (records) the agent version then runs the REAL dummy engine
    scripted by the node's `_want`. Mirrors app/flows/runtime.run_agent minus the DB."""
    async def run_agent(node):
        agent_id = node.get("agent_id")
        version_id = f"{agent_id}-v1"
        pins.append(version_id)  # stand-in for _pin_agent_version (DB pin is the unrun path)
        if raise_it:
            raise RuntimeError("engine blew up")
        spec = build_spec(agent_id, version_id, {
            "engine": "dummy",
            "tools": {"capture_lead": True},
            "dummy": {"port": node.get("_want")},
        })
        session = get_session_for_agent(spec)
        result = await session.run(spec, AgentCallContext(channel_id=CHAN, linkedid=LINKEDID))
        return (result.port, result.data)
    return run_agent


def _run(graph, run_agent=None):
    ari = FakeAri()
    rec = Recorder()
    interp = FlowInterpreter(graph=graph, channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, on_start=rec.pin, run_agent=run_agent)
    asyncio.run(interp.run())
    return ari, rec


def test_exits_by_each_port_and_pins():
    print("ai_agent node exits by the dummy's returned port + pins the version on entry:")
    for want, dest in (("transfer", "xfer"), ("end_call", "endc"), ("default", "dflt")):
        pins = []
        ari, rec = _run(_graph(want), _make_run_agent(pins))
        check(f"port '{want}' routed to '{dest}'", rec.node_ids() == ["start", "agent", dest])
        check(f"agent_version pinned exactly once for '{want}'", pins == ["A1-v1"])
        check(f"agent never bridged/dialled for '{want}'", "dial" not in ari.ops())
        check(f"session ran BEFORE routing onward for '{want}'",
              rec.node_ids().index("agent") < rec.node_ids().index(dest))


def test_failed_port_falls_to_fallback():
    print("dummy returns 'failed' (unwired) -> falls through to default_fallback (vm):")
    pins = []
    _, rec = _run(_graph("failed"), _make_run_agent(pins))
    check("failed (unwired) -> default_fallback vm", rec.node_ids() == ["start", "agent", "vm"])
    check("version still pinned before the failure fallback", pins == ["A1-v1"])


def test_run_agent_exception_routes_failed():
    print("run_agent that RAISES -> node takes 'failed' -> fallback (never dead-air):")
    pins = []
    ari, rec = _run(_graph("transfer"), _make_run_agent(pins, raise_it=True))
    check("raised session -> failed -> fallback vm", rec.node_ids() == ["start", "agent", "vm"])
    check("clean hangup, no bridge", ari.ops()[-1] == "hangup" and "dial" not in ari.ops())


def test_legacy_stub_without_seam():
    print("no run_agent injected -> legacy stub routes to 'default':")
    _, rec = _run(_graph("transfer"), None)  # want ignored; stub always -> default
    check("stub routed via 'default' port to dflt", rec.node_ids() == ["start", "agent", "dflt"])


def main():
    test_exits_by_each_port_and_pins()
    test_failed_port_falls_to_fallback()
    test_run_agent_exception_routes_failed()
    test_legacy_stub_without_seam()
    print("\nALL AI AGENT NODE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)

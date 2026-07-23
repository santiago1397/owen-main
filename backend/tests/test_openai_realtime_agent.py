"""Unit test for the openai_realtime VoiceAgentSession engine (Ticket 12).

Dependency-free by design (like test_voice_agent_seam / test_ai_agent_node): exercises the
PURE, import-light parts of app.agents.openai_realtime with FAKES for the OpenAI-WS /
AudioSocket transport and the SMS/DB side-effects. NO real audio/WS/DB — the sandbox can't run
those; the AudioSocket↔OpenAI-WS pump + the `transcriptions` write are the reviewed-not-run I/O
paths (marked `pragma: no cover` in the engine).

Asserts:
- tool→port mapping: transfer/end_call exit ports; capture_lead → data["captured"];
  send_sms enqueues to data["sms_outbox"] (and calls an injected sender when present);
  disabled/unknown tools are refused (no port, error result to the model);
- guardrail termination: max_call_seconds and max_silence_seconds both end on `end_call`;
- failure → 1 reconnect retry → `failed` (never dead air), and a retry that then SUCCEEDS wins;
- transcript assembly: speaker-labeled segments + flat text; persist called with the transcript;
- clean end (stream closes with no exit tool) → `default`;
- kill-switch/selection: the engine is the one the registry returns for "openai_realtime".

Run: python -m tests.test_openai_realtime_agent
"""

import asyncio
import sys

from app.agents.session import AgentCallContext, get_voice_agent_session
from app.agents.service import build_spec
from app.agents.openai_realtime import (
    Guardrails,
    OpenAIRealtimeSession,
    TranscriptAssembler,
    build_realtime_tools,
    dispatch_tool,
    guardrail_port,
    parse_guardrails,
    should_retry,
)
from app.agents.tools import enabled_tools


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"openai_realtime failed at: {name}")


CTX = AgentCallContext(channel_id="chan-1", linkedid="169.1", caller_number="+15551234567")


class FakeConn:
    """A fake RealtimeConnection: yields a scripted list of normalized events then None (clean
    end). Records tool-result callbacks so we can assert the in-call loop replies to the model."""

    def __init__(self, events, *, fail_on=None):
        self._events = list(events)
        self._i = 0
        self.tool_results = []
        self.closed = False
        self._fail_on = fail_on  # raise RealtimeConnectionError at this index

    async def next_event(self):
        if self._fail_on is not None and self._i == self._fail_on:
            from app.agents.openai_realtime import RealtimeConnectionError
            raise RealtimeConnectionError("boom")
        if self._i >= len(self._events):
            return None
        ev = self._events[self._i]
        self._i += 1
        return ev

    async def send_tool_result(self, call_id, result):
        self.tool_results.append((call_id, result))

    async def close(self):
        self.closed = True


def _spec(tools=None, guardrails=None):
    return build_spec("A1", "A1-v1", {
        "engine": "openai_realtime",
        "persona": "You are a helpful receptionist.",
        "greeting": "Thanks for calling!",
        "tools": tools or {},
        "guardrails": guardrails or {},
    })


def _session(events, *, spec=None, fail_first=False, persist_rec=None, sms_rec=None,
             max_reconnects=1, clock=None):
    spec = spec or _spec()
    conns = []

    async def connect(_spec, _ctx):
        # If fail_first, the FIRST connection raises during drive; the reconnect succeeds.
        idx = len(conns)
        if fail_first and idx == 0:
            c = FakeConn(events, fail_on=0)
        else:
            c = FakeConn(events)
        conns.append(c)
        return c

    async def persist(ctx, assembler, spec_):
        if persist_rec is not None:
            persist_rec.append((assembler.text(), assembler.segments()))

    async def sms_sender(to, body):
        if sms_rec is not None:
            sms_rec.append((to, body))
        return True

    sess = OpenAIRealtimeSession(
        connect=connect,
        persist=persist,
        monotonic=clock or (lambda: 0.0),
        max_reconnects=max_reconnects,
        sms_sender=sms_sender if sms_rec is not None else None,
    )
    result = asyncio.run(sess.run(spec, CTX))
    return result, conns


# --- pure helpers -------------------------------------------------------------------------

def test_guardrail_and_retry_pure():
    print("pure guardrail + retry decisions:")
    none = Guardrails()
    check("no limits -> no guardrail port", guardrail_port(9999, 9999, none) is None)
    lim = Guardrails(max_call_seconds=60, max_silence_seconds=10)
    check("under limits -> None", guardrail_port(30, 5, lim) is None)
    check("max_call tripped -> end_call", guardrail_port(60, 0, lim) == "end_call")
    check("max_silence tripped -> end_call", guardrail_port(1, 10, lim) == "end_call")
    g = parse_guardrails({"max_call_seconds": "120", "max_silence_seconds": 15, "model": "gpt"})
    check("parse coerces numerics", g.max_call_seconds == 120.0 and g.max_silence_seconds == 15.0)
    check("parse keeps model tier", g.model == "gpt")
    bad = parse_guardrails({"max_call_seconds": "abc", "max_silence_seconds": -3})
    check("garbage/negative limits -> unset", bad.max_call_seconds is None and bad.max_silence_seconds is None)
    check("should_retry: attempt 0 of 1 -> retry", should_retry(0, 1) is True)
    check("should_retry: attempt 1 of 1 -> stop", should_retry(1, 1) is False)


def test_tool_dispatch_pure():
    print("pure tool dispatch -> ports / side-effects:")
    en = enabled_tools({"transfer": True, "end_call": True, "capture_lead": True, "send_sms": True})
    o = dispatch_tool("transfer", {}, en, _spec(), CTX)
    check("transfer -> exit_port transfer", o.exit_port == "transfer")
    o = dispatch_tool("end_call", {"reason": "done"}, en, _spec(), CTX)
    check("end_call -> exit_port end_call", o.exit_port == "end_call")
    o = dispatch_tool("capture_lead", {"name": "Sam", "intent": "roof", "email": ""}, en, _spec(), CTX)
    check("capture_lead -> data.captured, no exit", o.exit_port is None and o.data["captured"]["name"] == "Sam")
    check("capture_lead drops blank fields", "email" not in o.data["captured"])
    o = dispatch_tool("send_sms", {"body": "hi"}, en, _spec(), CTX)
    check("send_sms defaults `to` to caller number", o.data["sms_outbox"][0]["to"] == CTX.caller_number)
    o = dispatch_tool("send_sms", {}, en, _spec(), CTX)
    check("send_sms without body -> error, no outbox", o.exit_port is None and "error" in o.result and not o.data)
    # disabled + unknown tools are refused.
    only_transfer = enabled_tools({"transfer": True})
    o = dispatch_tool("capture_lead", {}, only_transfer, _spec(), CTX)
    check("disabled tool refused (error result, no port/data)", "error" in o.result and o.exit_port is None and not o.data)
    o = dispatch_tool("nonsense", {}, en, _spec(), CTX)
    check("unknown tool refused", "error" in o.result)


def test_transcript_assembly_and_tools_schema():
    print("transcript assembly + realtime tool schema:")
    a = TranscriptAssembler()
    a.add("caller", "  Hello there  ")
    a.add("agent", "Hi, how can I help?")
    a.add("caller", "")  # blank ignored
    segs = a.segments()
    check("segments speaker-labeled in order",
          [s["speaker"] for s in segs] == ["caller", "agent"] and segs[0]["text"] == "Hello there")
    check("flat text is speaker-prefixed", a.text() == "caller: Hello there\nagent: Hi, how can I help?")
    check("blank fragment dropped", len(segs) == 2)
    schema = build_realtime_tools(enabled_tools({"transfer": True, "capture_lead": True}))
    names = {t["name"] for t in schema}
    check("only enabled tools in schema", names == {"transfer", "capture_lead"})
    check("schema entries are function tools", all(t["type"] == "function" for t in schema))


# --- the drive loop (with fakes) ----------------------------------------------------------

def test_tool_exit_ports_via_loop():
    print("drive loop: tool calls exit by the right port:")
    for tool, port in (("transfer", "transfer"), ("end_call", "end_call")):
        res, conns = _session([
            {"type": "speech", "speaker": "caller", "text": "please help", "at": 1.0},
            {"type": "tool_call", "name": tool, "arguments": {}, "call_id": "c1", "at": 2.0},
        ], spec=_spec(tools={tool: True}))
        check(f"{tool} tool -> port {port}", res.port == port)
        check(f"{tool} closed the connection", conns[-1].closed is True)


def test_capture_lead_and_sms_via_loop():
    print("drive loop: capture_lead -> data.captured; send_sms enqueues + calls sender; then end:")
    sms_rec = []
    res, conns = _session([
        {"type": "tool_call", "name": "capture_lead",
         "arguments": {"name": "Dana", "intent": "quote"}, "call_id": "c1", "at": 1.0},
        {"type": "tool_call", "name": "send_sms",
         "arguments": {"body": "We'll text you"}, "call_id": "c2", "at": 2.0},
        {"type": "tool_call", "name": "end_call", "arguments": {}, "call_id": "c3", "at": 3.0},
    ], spec=_spec(tools={"capture_lead": True, "send_sms": True, "end_call": True}), sms_rec=sms_rec)
    check("captured surfaced in data", res.data.get("captured", {}).get("name") == "Dana")
    check("sms enqueued to outbox", res.data["sms_outbox"][0]["body"] == "We'll text you")
    check("injected sms sender invoked", sms_rec == [(CTX.caller_number, "We'll text you")])
    check("in-call tools replied to the model (2 results before exit)", len(conns[-1].tool_results) == 2)
    check("final port is end_call", res.port == "end_call")


def test_clean_end_is_default():
    print("drive loop: stream closes with no exit tool -> default:")
    res, _ = _session([
        {"type": "speech", "speaker": "caller", "text": "bye", "at": 1.0},
        {"type": "speech", "speaker": "agent", "text": "goodbye", "at": 2.0},
    ], spec=_spec(tools={"end_call": True}))
    check("no exit tool + clean close -> default", res.port == "default")


def test_guardrail_termination_via_loop():
    print("drive loop: guardrails end the call gracefully on end_call:")
    # max_call: event at t=60 with a 60s cap.
    res, _ = _session([
        {"type": "speech", "speaker": "caller", "text": "hi", "at": 0.0},
        {"type": "tick", "at": 60.0},
        {"type": "tool_call", "name": "transfer", "arguments": {}, "call_id": "x", "at": 61.0},
    ], spec=_spec(tools={"transfer": True}, guardrails={"max_call_seconds": 60}))
    check("max_call_seconds -> end_call (before the transfer tool)", res.port == "end_call")
    # max_silence: last activity at t=0, tick at t=30 with a 20s silence cap.
    res, _ = _session([
        {"type": "speech", "speaker": "caller", "text": "hi", "at": 0.0},
        {"type": "tick", "at": 30.0},
        {"type": "tool_call", "name": "transfer", "arguments": {}, "call_id": "x", "at": 31.0},
    ], spec=_spec(tools={"transfer": True}, guardrails={"max_silence_seconds": 20}))
    check("max_silence_seconds -> end_call", res.port == "end_call")


def test_failure_retry_then_failed():
    print("drive loop: transport error -> 1 reconnect retry -> failed (never dead air):")
    # An EV_ERROR event makes every attempt fail deterministically; with max_reconnects=1 that
    # is 2 attempts (initial + one reconnect), after which the session returns the `failed` port.
    res, conns = _session([{"type": "error", "message": "ws drop"}], max_reconnects=1)
    check("both attempts errored -> failed port", res.port == "failed")
    check("exactly 2 connection attempts (initial + 1 retry)", len(conns) == 2)


def test_retry_then_success_wins():
    print("drive loop: first attempt drops, reconnect SUCCEEDS -> that result wins:")
    res, conns = _session([
        {"type": "tool_call", "name": "end_call", "arguments": {}, "call_id": "c1", "at": 1.0},
    ], spec=_spec(tools={"end_call": True}), fail_first=True, max_reconnects=1)
    check("reconnect succeeded -> end_call", res.port == "end_call")
    check("two connections opened (drop + reconnect)", len(conns) == 2)


def test_transcript_persisted():
    print("transcript assembled speaker-labeled and persisted inline:")
    persist_rec = []
    res, _ = _session([
        {"type": "speech", "speaker": "caller", "text": "I need a roof quote", "at": 1.0},
        {"type": "speech", "speaker": "agent", "text": "Sure, what's your address?", "at": 2.0},
        {"type": "tool_call", "name": "end_call", "arguments": {}, "call_id": "c1", "at": 3.0},
    ], spec=_spec(tools={"end_call": True}), persist_rec=persist_rec)
    check("persist called once with a transcript", len(persist_rec) == 1)
    text, segs = persist_rec[0]
    check("transcript text speaker-prefixed",
          text == "caller: I need a roof quote\nagent: Sure, what's your address?")
    check("segments carry both speakers", [s["speaker"] for s in segs] == ["caller", "agent"])


def test_registry_selection():
    print("registry: openai_realtime resolves to the real engine:")
    eng = get_voice_agent_session("openai_realtime")
    check("registry returns OpenAIRealtimeSession", isinstance(eng, OpenAIRealtimeSession))
    check("engine name", eng.name == "openai_realtime")


def main():
    test_guardrail_and_retry_pure()
    test_tool_dispatch_pure()
    test_transcript_assembly_and_tools_schema()
    test_tool_exit_ports_via_loop()
    test_capture_lead_and_sms_via_loop()
    test_clean_end_is_default()
    test_guardrail_termination_via_loop()
    test_failure_retry_then_failed()
    test_retry_then_success_wins()
    test_transcript_persisted()
    test_registry_selection()
    print("\nALL OPENAI_REALTIME AGENT CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)

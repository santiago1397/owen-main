"""Unit test for the in-memory ARI flow interpreter (Ticket 07).

Dependency-free by design (like test_flow_validator / test_asterisk_ingestion): exercises the
PURE app.flows.interpreter with a FAKE ARI client, a fake emit(), a fake clock, and an
in-memory flow-version graph. It does NOT hit Postgres/ARI (sqlalchemy + httpx aren't in this
sandbox — the interpreter core deliberately imports neither).

Asserts:
- one call_event emitted per node transition, in order;
- the flow_version is pinned at StasisStart (on_start runs once, before any transition);
- a menu DTMF routes to the correct wired port;
- an unwired digit and an errored node both fall through to default_fallback (never dead air);
- a missing default_fallback dead-end hangs up cleanly;
- voicemail and hangup terminate the call (drive record/hangup on the ARI client);
- dial routes on its result port and honours the `record` modifier;
- evaluate_hours (pure) picks open vs closed.

Run: python -m tests.test_flow_interpreter
"""

import asyncio
import sys
from datetime import datetime, timezone

from app.flows.interpreter import FlowInterpreter, evaluate_hours


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"flow_interpreter failed at: {name}")


LINKEDID = "1690000000.1"
CHAN = "1690000000.1"


class FakeAri:
    """Records every control op; scriptable DTMF digit and dial result."""

    def __init__(self, digit=None, dial_result="answered"):
        self.calls = []          # ordered (op, channel_id, arg)
        self.record_names = []   # record_name passed to each dial (None = no recording)
        self._digit = digit
        self._dial_result = dial_result

    async def answer(self, channel_id):
        self.calls.append(("answer", channel_id, None))

    async def play(self, channel_id, media):
        self.calls.append(("play", channel_id, media))

    async def record(self, channel_id, name):
        self.calls.append(("record", channel_id, name))

    async def read_digit(self, channel_id, *, prompt, timeout_s, max_digits):
        self.calls.append(("read_digit", channel_id, prompt))
        return self._digit

    async def dial_number(self, channel_id, number, *, caller_id, timeout_s, record_name=None):
        # `record_name` (not a pre-dial channel record) is how a dial node asks for recording:
        # the client records the BRIDGE once both legs are joined.
        self.calls.append(("dial", channel_id, number))
        self.record_names.append(record_name)
        return self._dial_result

    async def dial_operator(self, channel_id, operators, *, caller_id, timeout_s, record_name=None):
        # Record the resolved operator list; reuse the scripted dial result (Ticket 13).
        self.calls.append(("dial_operator", channel_id, tuple(operators)))
        self.record_names.append(record_name)
        return self._dial_result

    async def voicemail(self, channel_id, *, greeting, name, max_duration_s, max_silence_s):
        # Ticket 18: real voicemail is a single blocking op (greeting+beep+record+hangup) —
        # the interpreter delegates the whole capture to the ARI client.
        self.calls.append(("voicemail", channel_id, greeting))

    async def hangup(self, channel_id):
        self.calls.append(("hangup", channel_id, None))

    def ops(self):
        return [c[0] for c in self.calls]


class Recorder:
    """Captures emitted call_events and on_start (the version pin).

    The interpreter emits THREE kinds of event and the assertions below care about different
    ones, so they are split here rather than at every call site:
      - ENTRY  `flow.node.<type>` — one per node entered (the original transition stream);
      - EXIT   `flow.node.exit`   — one per node left, carrying the port/routing outcome;
      - SUMMARY `flow.call.summary` — exactly one per call, with the whole path.
    `entries()` is what "one event per transition" has always meant, so the pre-existing
    checks keep asserting on it unchanged.
    """

    def __init__(self):
        self.events = []      # (event_type, provider_sequence, payload) — every kind
        self.started = 0

    async def emit(self, event_type, seq, payload):
        self.events.append((event_type, seq, payload))

    async def pin(self):
        # Records that the pin happened, and how many events preceded it (must be 0).
        self.started += 1
        self._events_before_pin = len(self.events)

    def entries(self):
        return [e for e in self.events
                if e[0].startswith("flow.node.") and e[0] != "flow.node.exit"]

    def exits(self):
        return [e for e in self.events if e[0] == "flow.node.exit"]

    def summaries(self):
        return [e for e in self.events if e[0] == "flow.call.summary"]

    def summary(self):
        """The single summary payload's `flow` dict (asserts there is exactly one)."""
        rows = self.summaries()
        assert len(rows) == 1, f"expected exactly 1 summary, got {len(rows)}"
        return rows[0][2]["flow"]

    def exit_for(self, node_id):
        """The exit payload's `flow` dict for `node_id` (None if the node never exited)."""
        for e in self.exits():
            if e[2]["flow"]["node_id"] == node_id:
                return e[2]["flow"]
        return None

    def types(self):
        return [e[0] for e in self.entries()]

    def node_ids(self):
        return [e[2]["flow"]["node_id"] for e in self.entries()]


def _run(interp):
    asyncio.run(interp.run())


# A greeting-then-IVR graph exercising entry/hours/play(record)/menu/dial/voicemail/hangup.
def _graph():
    return {
        "default_fallback": "vm",
        "nodes": {
            "start": {"type": "entry", "next": {"default": "hrs"}},
            "hrs": {"type": "hours", "next": {"open": "greet", "closed": "vm"}},
            "greet": {"type": "play", "media": "sound:welcome", "record": True, "next": {"default": "menu"}},
            "menu": {"type": "menu", "media": "sound:ivr", "next": {"1": "sales", "2": "agent"}},
            "sales": {"type": "dial", "target": "+13055550000", "record": True,
                      "next": {"answered": "bye", "busy": "vm", "noanswer": "vm", "failed": "vm"}},
            "agent": {"type": "ai_agent", "next": {"default": "bye"}},
            "vm": {"type": "voicemail", "media": "sound:leave-msg", "next": {"default": "bye"}},
            "bye": {"type": "hangup"},
        },
    }


ALWAYS_OPEN = lambda: datetime(2026, 7, 22, 16, 0, tzinfo=timezone.utc)  # noqa: E731


def test_happy_path_pins_and_emits_one_per_transition():
    print("happy path — pin at StasisStart + one call_event per transition, menu digit 1 -> dial:")
    ari = FakeAri(digit="1", dial_result="answered")
    rec = Recorder()
    interp = FlowInterpreter(graph=_graph(), channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, on_start=rec.pin)
    _run(interp)

    check("on_start (version pin) ran exactly once", rec.started == 1)
    check("pin happened at StasisStart, before any transition emit", rec._events_before_pin == 0)
    # entry -> hrs(open) -> greet -> menu(1) -> sales(dial answered) -> bye(hangup)
    check("visited nodes in order", rec.node_ids() == ["start", "hrs", "greet", "menu", "sales", "bye"])
    check("one entry event per transition (6)", len(rec.entries()) == 6)
    check("event types are flow.node.<type>",
          rec.types() == ["flow.node.entry", "flow.node.hours", "flow.node.play",
                          "flow.node.menu", "flow.node.dial", "flow.node.hangup"])
    check("dedup key is {linkedid}:{step}:{node_id}", rec.entries()[0][1] == f"{LINKEDID}:1:start")
    check("entry answered the channel", ari.ops()[0] == "answer")
    check("play node with record modifier recorded then played",
          ("record", CHAN, f"{LINKEDID}-play-1") in ari.calls and ("play", CHAN, "sound:welcome") in ari.calls)
    # A dial node's `record` asks the client to record the BRIDGE (via record_name) — it must
    # NOT start a channel recording first, which makes ARI reject the bridge (409) and leaves
    # the call on dead air. See tests/test_dial_record_bridge.py for the full contract.
    check("dial node with record modifier passed a bridge record_name, not a channel record",
          ari.record_names == [f"{LINKEDID}-dial-2"]
          and ("record", CHAN, f"{LINKEDID}-dial-2") not in ari.calls)
    check("dial placed to the NUMBER target", ("dial", CHAN, "+13055550000") in ari.calls)
    check("hangup terminated the call", ari.ops()[-1] == "hangup")


def test_menu_dtmf_routes_to_correct_port():
    print("menu DTMF routes to the pressed digit's wired port (2 -> ai_agent stub -> bye):")
    ari = FakeAri(digit="2")
    rec = Recorder()
    interp = FlowInterpreter(graph=_graph(), channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, on_start=rec.pin)
    _run(interp)
    check("digit 2 routed to ai_agent then its default -> bye",
          rec.node_ids() == ["start", "hrs", "greet", "menu", "agent", "bye"])
    check("ai_agent stub fell through to 'default' (bye)", "flow.node.ai_agent" in rec.types())


def test_unwired_digit_falls_to_default_fallback():
    print("unwired menu digit falls through to default_fallback (voicemail), which terminates:")
    ari = FakeAri(digit="9")  # 9 is not a wired port on the menu
    rec = Recorder()
    interp = FlowInterpreter(graph=_graph(), channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, on_start=rec.pin)
    _run(interp)
    check("unwired digit 9 -> default_fallback vm", rec.node_ids() == ["start", "hrs", "greet", "menu", "vm"])
    check("voicemail captured a message (greeting+beep+record+hangup, one op)",
          ari.ops()[-1] == "voicemail" and ("voicemail", CHAN, "sound:leave-msg") in ari.calls)


def test_errored_node_falls_to_default_fallback():
    print("a node whose handler ERRORS falls through to default_fallback:")

    class BoomAri(FakeAri):
        async def read_digit(self, *a, **k):
            raise RuntimeError("ARI blew up mid-menu")

    ari = BoomAri()
    rec = Recorder()
    interp = FlowInterpreter(graph=_graph(), channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, on_start=rec.pin)
    _run(interp)
    check("errored menu -> default_fallback vm -> terminate",
          rec.node_ids() == ["start", "hrs", "greet", "menu", "vm"])


def test_hours_closed_routes_to_closed_port():
    print("hours node CLOSED routes via the 'closed' port (to voicemail):")
    ari = FakeAri()
    rec = Recorder()
    closed_clock = lambda: datetime(2026, 7, 22, 3, 0, tzinfo=timezone.utc)  # noqa: E731
    g = _graph()
    g["nodes"]["hrs"]["hours"] = {"tz": "America/New_York", "schedule": {"wed": [["09:00", "17:00"]]}}
    interp = FlowInterpreter(graph=g, channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=closed_clock, on_start=rec.pin)
    _run(interp)
    check("closed (3am ET) -> vm", rec.node_ids() == ["start", "hrs", "vm"])


def test_missing_fallback_dead_end_hangs_up_cleanly():
    print("dead-end with NO default_fallback hangs up cleanly (never dead air):")
    g = {
        "nodes": {
            "start": {"type": "entry", "next": {"default": "menu"}},
            "menu": {"type": "menu", "next": {}},  # no options; timeout unwired; no fallback
        },
    }
    ari = FakeAri(digit=None)  # timeout
    rec = Recorder()
    interp = FlowInterpreter(graph=g, channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, on_start=rec.pin)
    _run(interp)
    check("visited entry then menu", rec.node_ids() == ["start", "menu"])
    check("clean hangup on unwired dead-end (no fallback)", ari.ops()[-1] == "hangup")


def test_dial_busy_routes_to_busy_port():
    print("dial result 'busy' routes via the 'busy' port to voicemail:")
    ari = FakeAri(digit="1", dial_result="busy")
    rec = Recorder()
    interp = FlowInterpreter(graph=_graph(), channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, on_start=rec.pin)
    _run(interp)
    check("dial busy -> vm -> terminate", rec.node_ids()[-2:] == ["sales", "vm"])


def _operator_graph(node):
    """A minimal entry -> dial(operator) -> [answered]->bye graph; `node` is the dial node."""
    return {
        "default_fallback": "vm",
        "nodes": {
            "start": {"type": "entry", "next": {"default": "op"}},
            "op": node,
            "vm": {"type": "voicemail", "next": {"default": "bye"}},
            "bye": {"type": "hangup"},
        },
    }


def test_operator_target_individual_dials_operator():
    print("dial operator-target (individual) -> dial_operator with the one operator, answered -> bye:")
    node = {"type": "dial", "target_kind": "operator", "operator": "jane@x.com",
            "next": {"answered": "bye", "noanswer": "vm"}}
    ari = FakeAri(dial_result="answered")
    rec = Recorder()
    interp = FlowInterpreter(graph=_operator_graph(node), channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, on_start=rec.pin)
    _run(interp)
    check("dial_operator called with [jane@x.com]", ("dial_operator", CHAN, ("jane@x.com",)) in ari.calls)
    check("answered routed to bye", rec.node_ids() == ["start", "op", "bye"])


def test_operator_target_group_dedups_and_routes():
    print("dial operator-target (group, mixed id/object shapes) dedups; answered -> bye:")
    node = {"type": "dial", "target_kind": "operator",
            "operators": ["jane@x.com", {"id": "bob@x.com"}, "jane@x.com"],
            "next": {"answered": "bye", "noanswer": "vm"}}
    ari = FakeAri(dial_result="answered")
    rec = Recorder()
    interp = FlowInterpreter(graph=_operator_graph(node), channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, on_start=rec.pin)
    _run(interp)
    check("group flattened + de-duplicated in order",
          ("dial_operator", CHAN, ("jane@x.com", "bob@x.com")) in ari.calls)
    check("answered routed to bye", rec.node_ids() == ["start", "op", "bye"])


def test_operator_target_no_answer_falls_to_default_fallback():
    print("operator no-answer (unavailable/unregistered) -> default_fallback (voicemail):")
    node = {"type": "dial", "target_kind": "operator", "operator": "jane@x.com",
            "next": {"answered": "bye"}}  # 'noanswer' unwired -> falls through
    ari = FakeAri(dial_result="noanswer")
    rec = Recorder()
    interp = FlowInterpreter(graph=_operator_graph(node), channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, on_start=rec.pin)
    _run(interp)
    check("no-answer falls through to default_fallback vm", rec.node_ids() == ["start", "op", "vm"])


def test_operator_target_empty_falls_to_default_fallback():
    print("operator-target with NO operators configured -> error -> default_fallback:")
    node = {"type": "dial", "target_kind": "operator", "operators": [], "next": {"answered": "bye"}}
    ari = FakeAri(dial_result="answered")
    rec = Recorder()
    interp = FlowInterpreter(graph=_operator_graph(node), channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, on_start=rec.pin)
    _run(interp)
    check("no dial_operator attempted", "dial_operator" not in ari.ops())
    check("errored operator target -> vm", rec.node_ids() == ["start", "op", "vm"])


def test_number_target_still_works():
    print("regression: a NUMBER-target dial still routes via dial_number (not dial_operator):")
    node = {"type": "dial", "target": "+13055550000",
            "next": {"answered": "bye", "busy": "vm", "noanswer": "vm", "failed": "vm"}}
    ari = FakeAri(dial_result="answered")
    rec = Recorder()
    interp = FlowInterpreter(graph=_operator_graph(node), channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, on_start=rec.pin)
    _run(interp)
    check("dial_number used for a number target", ("dial", CHAN, "+13055550000") in ari.calls)
    check("dial_operator NOT used", "dial_operator" not in ari.ops())


# --- Ticket 17 parity nodes ---------------------------------------------------------------


def _flow_of(interp_kwargs, graph, ari=None):
    ari = ari or FakeAri()
    rec = Recorder()
    interp = FlowInterpreter(graph=graph, channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, on_start=rec.pin, **interp_kwargs)
    _run(interp)
    return ari, rec, interp


def test_set_vars_interpolation_and_snapshot():
    print("set_vars + {{var}} interpolation into a play prompt; event snapshots values:")
    g = {
        "default_fallback": "vm",
        "nodes": {
            "start": {"type": "entry", "next": {"default": "setv"}},
            "setv": {"type": "set_vars", "vars": {"who": "{{caller_number}}", "n": 7},
                     "next": {"default": "greet"}},
            "greet": {"type": "play", "prompt": "Hi {{who}}, you are caller {{n}}. {{nope}}",
                      "next": {"default": "bye"}},
            "vm": {"type": "voicemail", "next": {}},
            "bye": {"type": "hangup"},
        },
    }
    ari, rec, interp = _flow_of({"variables": {"caller_number": "+13055550123"}}, g)
    check("visited set_vars then play then bye", rec.node_ids() == ["start", "setv", "greet", "bye"])
    check("play prompt interpolated (unknown var -> empty)",
          ("play", CHAN, "Hi +13055550123, you are caller 7. ") in ari.calls)
    setv_evt = rec.entries()[1]
    check("set_vars event snapshots names+values",
          setv_evt[2]["flow"].get("vars_set") == {"who": "+13055550123", "n": "7"})
    check("non-string literal stored as-is", interp.variables["n"] == 7)


def test_unset_vars_removes_names():
    print("unset_vars removes listed vars; event lists what was removed:")
    g = {
        "default_fallback": "bye",
        "nodes": {
            "start": {"type": "entry", "next": {"default": "unsetv"}},
            "unsetv": {"type": "unset_vars", "names": ["a", "missing"], "next": {"default": "bye"}},
            "bye": {"type": "hangup"},
        },
    }
    _, rec, interp = _flow_of({"variables": {"a": "1", "b": "2"}}, g)
    check("'a' removed, 'b' kept", "a" not in interp.variables and interp.variables.get("b") == "2")
    check("event lists removed names only", rec.entries()[1][2]["flow"].get("vars_unset") == ["a"])


def _conditions_graph(rows):
    return {
        "default_fallback": "vm",
        "nodes": {
            "start": {"type": "entry", "next": {"default": "cond"}},
            "cond": {"type": "conditions", "rows": rows,
                     "next": {"m1": "one", "m2": "two", "else": "other"}},
            "one": {"type": "hangup"},
            "two": {"type": "hangup"},
            "other": {"type": "hangup"},
            "vm": {"type": "voicemail", "next": {}},
        },
    }


def test_conditions_routing_and_gather_digits():
    print("conditions node: first match wins; gather.digits set by menu; else on no match:")
    rows = [
        {"variable": "gather.digits", "operator": "equals", "value": "9", "port": "m1"},
        {"variable": "gather.digits", "operator": "equals", "value": "1", "port": "m2"},
    ]
    g = _conditions_graph(rows)
    # Put a menu in front so gather.digits is populated by the flow itself.
    g["nodes"]["start"]["next"]["default"] = "menu"
    g["nodes"]["menu"] = {"type": "menu", "next": {"1": "cond", "timeout": "vm"}}
    ari, rec, interp = _flow_of({}, g, FakeAri(digit="1"))
    check("menu set gather.digits", interp.variables.get("gather.digits") == "1")
    check("second row matched -> port m2 -> two",
          rec.node_ids() == ["start", "menu", "cond", "two"])
    cond_evt = rec.entries()[2][2]["flow"]
    check("event snapshots matched row + port + actual",
          cond_evt.get("matched_row") == 1 and cond_evt.get("port") == "m2"
          and cond_evt.get("actual") == "1")

    _, rec, _ = _flow_of({"variables": {"gather.digits": "5"}}, _conditions_graph(rows))
    check("no match -> else port", rec.node_ids() == ["start", "cond", "other"])

    bad = [{"variable": "gather.digits", "operator": "regex", "value": "([", "port": "m1"},
           {"variable": "gather.digits", "operator": "equals", "value": "5", "port": "m2"}]
    _, rec, _ = _flow_of({"variables": {"gather.digits": "5"}}, _conditions_graph(bad))
    check("bad regex row skipped; next row matches", rec.node_ids() == ["start", "cond", "two"])


def test_send_sms_fire_and_forget():
    print("send_sms node: interpolated to/body through the seam; default port regardless:")
    sent = []

    async def sender(to, body):
        sent.append((to, body))
        return True

    g = {
        "default_fallback": "vm",
        "nodes": {
            "start": {"type": "entry", "next": {"default": "sms"}},
            "sms": {"type": "send_sms", "body": "Thanks {{caller_number}}!", "next": {"default": "bye"}},
            "vm": {"type": "voicemail", "next": {}},
            "bye": {"type": "hangup"},
        },
    }
    _, rec, _ = _flow_of({"variables": {"caller_number": "+13055550123"},
                          "send_sms": sender}, g)
    check("sender got interpolated to (default {{caller_number}}) + body",
          sent == [("+13055550123", "Thanks +13055550123!")])
    check("default port taken -> bye", rec.node_ids() == ["start", "sms", "bye"])
    check("event snapshots to/body", rec.entries()[1][2]["flow"].get("sms_to") == "+13055550123")

    async def boom(to, body):
        raise RuntimeError("carrier down")

    _, rec, _ = _flow_of({"variables": {"caller_number": "+13055550123"}, "send_sms": boom}, g)
    check("sender failure still takes default port", rec.node_ids() == ["start", "sms", "bye"])

    _, rec, _ = _flow_of({"variables": {"caller_number": "+13055550123"}}, g)
    check("no seam still takes default port", rec.node_ids() == ["start", "sms", "bye"])


def test_request_node_success_failure_and_dot_path():
    print("request node: 2xx -> success + request.body.* readable; error -> failure + status 0:")
    seen = []

    async def http_ok(method, url, headers, body):
        seen.append((method, url, headers, body))
        return 200, {"data": {"status": "open"}}

    g = {
        "default_fallback": "vm",
        "nodes": {
            "start": {"type": "entry", "next": {"default": "req"}},
            "req": {"type": "request", "method": "POST", "url": "https://x.test/{{caller_number}}",
                    "headers": {"X-Caller": "{{caller_number}}"}, "body": "{\"n\": \"{{caller_number}}\"}",
                    "next": {"success": "cond", "failure": "fail"}},
            "cond": {"type": "conditions",
                     "rows": [{"variable": "request.body.data.status", "operator": "equals",
                               "value": "open", "port": "m1"}],
                     "next": {"m1": "ok", "else": "other"}},
            "ok": {"type": "hangup"},
            "other": {"type": "hangup"},
            "fail": {"type": "hangup"},
            "vm": {"type": "voicemail", "next": {}},
        },
    }
    _, rec, interp = _flow_of({"variables": {"caller_number": "+1305"}, "http_request": http_ok}, g)
    check("url/headers/body interpolated",
          seen == [("POST", "https://x.test/+1305", {"X-Caller": "+1305"}, '{"n": "+1305"}')])
    check("2xx -> success; dot-path condition matched",
          rec.node_ids() == ["start", "req", "cond", "ok"])
    check("request.status stored", interp.variables.get("request.status") == 200)
    check("request event snapshots status", rec.entries()[1][2]["flow"].get("request_status") == 200)

    async def http_500(method, url, headers, body):
        return 500, {"error": "boom"}

    _, rec, interp = _flow_of({"variables": {"caller_number": "+1305"}, "http_request": http_500}, g)
    check("non-2xx -> failure port", rec.node_ids() == ["start", "req", "fail"])
    check("failed status stored", interp.variables.get("request.status") == 500)

    async def http_boom(method, url, headers, body):
        raise RuntimeError("transport down")

    _, rec, interp = _flow_of({"variables": {"caller_number": "+1305"}, "http_request": http_boom}, g)
    check("transport error -> failure port with status 0",
          rec.node_ids() == ["start", "req", "fail"] and interp.variables.get("request.status") == 0)

    _, rec, _ = _flow_of({"variables": {"caller_number": "+1305"}}, g)
    check("no http seam -> failure port", rec.node_ids() == ["start", "req", "fail"])


def test_runtime_builtins_seeded():
    print("built-in variables seeded via the constructor reach interpolation:")
    g = {
        "default_fallback": "bye",
        "nodes": {
            "start": {"type": "entry", "next": {"default": "greet"}},
            "greet": {"type": "play", "prompt": "You called {{dialed_number}} at {{call.time}} on {{call.dow}}",
                      "next": {"default": "bye"}},
            "bye": {"type": "hangup"},
        },
    }
    ari, _, _ = _flow_of({"variables": {"dialed_number": "+13055559999", "call.time": "14:05",
                                        "call.dow": "wed"}}, g)
    check("built-ins interpolate into the prompt",
          ("play", CHAN, "You called +13055559999 at 14:05 on wed") in ari.calls)


def test_evaluate_hours_pure():
    print("evaluate_hours (pure) — open window vs outside window vs no-schedule fail-open:")
    node = {"type": "hours", "hours": {"tz": "America/New_York", "schedule": {"wed": [["09:00", "17:00"]]}}}
    open_now = datetime(2026, 7, 22, 16, 0, tzinfo=timezone.utc)   # Wed 12:00 ET -> open
    closed_now = datetime(2026, 7, 22, 3, 0, tzinfo=timezone.utc)  # Tue 23:00 ET -> closed
    check("inside window -> open", evaluate_hours(node, open_now, "UTC") is True)
    check("outside window -> closed", evaluate_hours(node, closed_now, "UTC") is False)
    check("no schedule -> fail open", evaluate_hours({"type": "hours"}, closed_now, "UTC") is True)


def test_exit_events_record_port_and_routing():
    print("exit events record the port taken, where it routed, and how long the node took:")
    ari = FakeAri(digit="1", dial_result="answered")
    rec = Recorder()
    # Deterministic monotonic: every call advances 0.25s, so `ms` is assertable.
    ticks = iter([i * 0.25 for i in range(200)])
    interp = FlowInterpreter(graph=_graph(), channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, on_start=rec.pin,
                             monotonic=lambda: next(ticks))
    _run(interp)

    check("one exit event per node entered (6)", len(rec.exits()) == 6)
    check("exit dedup key is {linkedid}:{step}:{node_id}:exit",
          rec.exits()[0][1] == f"{LINKEDID}:1:start:exit")
    menu = rec.exit_for("menu")
    check("menu exit records the digit as its port", menu["port"] == "1")
    check("menu exit records the wired target it routed to",
          menu["routed"] == "edge" and menu["next"] == "sales")
    check("menu exit snapshots the digits pressed", menu["digits"] == "1")
    check("menu exit records the options the caller COULD press", menu["options"] == ["1", "2"])
    dial = rec.exit_for("sales")
    check("dial exit records target and result",
          dial["dial_target"] == "+13055550000" and dial["dial_result"] == "answered")
    check("dial exit records that the bridge was recorded", dial["recorded"] is True)
    check("terminal node exit is marked terminal", rec.exit_for("bye")["routed"] == "terminal")
    check("node duration is measured from the injected monotonic", menu["ms"] == 250)
    hrs = rec.exit_for("hrs")
    check("hours exit records open + tz", hrs["hours_open"] is True and hrs["hours_tz"])


def test_menu_timeout_is_distinguishable_from_invalid_digit():
    print("menu timeout vs unwired digit are distinguishable from the exit event alone:")
    # This is the production case that motivated the exit event: a live flow whose menu
    # timeout/invalid ports both pointed at a hangup node, so every dropped caller looked
    # identical in the event log and the reason was only recoverable from the graph JSON.
    g = {
        "nodes": {
            "start": {"type": "entry", "next": {"default": "menu"}},
            "menu": {"type": "menu", "media": "sound:ivr",
                     "next": {"1": "bye", "invalid": "drop", "timeout": "drop"}},
            "drop": {"type": "hangup"},
            "bye": {"type": "hangup"},
        },
    }
    rec = Recorder()
    interp = FlowInterpreter(graph=g, channel_id=CHAN, ari=FakeAri(digit=None), emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN)
    _run(interp)
    menu = rec.exit_for("menu")
    check("no input -> port 'timeout'", menu["port"] == "timeout")
    check("no input -> digits null (not empty string)", menu["digits"] is None)
    check("timeout routed to the wired drop node", menu["next"] == "drop")
    check("the timeout the caller was given is recorded", menu["timeout_s"] == 5)

    rec2 = Recorder()
    interp2 = FlowInterpreter(graph=g, channel_id=CHAN, ari=FakeAri(digit="9"), emit=rec2.emit,
                              linkedid=LINKEDID, now=ALWAYS_OPEN)
    _run(interp2)
    menu2 = rec2.exit_for("menu")
    check("unwired digit -> port 'invalid'", menu2["port"] == "invalid")
    check("unwired digit is preserved in the payload", menu2["digits"] == "9")
    check("same terminal node, DIFFERENT recorded reason", menu["port"] != menu2["port"])


def test_call_summary_records_path_and_end_reason():
    print("flow.call.summary records the whole path and why the call ended:")
    ari = FakeAri(digit="1", dial_result="answered")
    rec = Recorder()
    interp = FlowInterpreter(graph=_graph(), channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, on_start=rec.pin)
    _run(interp)
    s = rec.summary()
    check("path is the nodes in order", s["path"] == ["start", "hrs", "greet", "menu", "sales", "bye"])
    check("terminal node is the last one entered", s["terminal_node"] == "bye")
    check("step count matches the path", s["steps"] == 6)
    check("ended on a terminal node", s["ended"] == "terminal")
    check("summary dedup key is one-per-call", rec.summaries()[0][1] == f"{LINKEDID}:summary")


def test_summary_names_the_caller_dropped_case():
    print("a caller dropped by an unwired port with no fallback is named in the summary:")
    g = {
        "nodes": {
            "start": {"type": "entry", "next": {"default": "menu"}},
            "menu": {"type": "menu", "next": {}},  # no options, no timeout port, no fallback
        },
    }
    rec = Recorder()
    interp = FlowInterpreter(graph=g, channel_id=CHAN, ari=FakeAri(digit=None), emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN)
    _run(interp)
    check("ended reason distinguishes a DROP from a deliberate hangup node",
          rec.summary()["ended"] == "unrouted_hangup")
    check("the menu's exit says it hung up rather than routing",
          rec.exit_for("menu")["routed"] == "hangup")
    check("no fallback recorded as the next hop", rec.exit_for("menu")["next"] is None)


def test_truncated_path_keeps_step_count_and_terminal_node_true():
    print("a runaway loop truncates the path but not the step count or terminal node:")
    # A self-referential graph: the interpreter's max_steps guard stops it. The path caps at
    # _PATH_MAX, so steps/terminal_node must NOT be derived from the path — a 100-hop loop
    # reported as a tidy 50-step call would hide exactly the pathology worth seeing.
    g = {
        "nodes": {
            "start": {"type": "entry", "next": {"default": "loop"}},
            "loop": {"type": "play", "media": "sound:x", "next": {"default": "loop"}},
        },
    }
    rec = Recorder()
    interp = FlowInterpreter(graph=g, channel_id=CHAN, ari=FakeAri(), emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN, max_steps=60)
    _run(interp)
    s = rec.summary()
    check("ended on the max_steps guard", s["ended"] == "max_steps")
    check("path is capped", len(s["path"]) == 50)
    check("path is flagged as truncated", s.get("path_truncated") is True)
    check("step count is the TRUE number of hops, not the capped path length",
          s["steps"] == 60 and s["steps"] > len(s["path"]))
    check("terminal node is the node it actually ended on", s["terminal_node"] == "loop")


def test_summary_is_emitted_even_when_a_node_explodes():
    print("the summary still lands when the interpreter itself raises:")

    class Exploding(FakeAri):
        async def answer(self, channel_id):
            raise RuntimeError("ARI is down")

    rec = Recorder()
    g = {"nodes": {"start": {"type": "entry", "next": {"default": "bye"}},
                   "bye": {"type": "hangup"}}}
    interp = FlowInterpreter(graph=g, channel_id=CHAN, ari=Exploding(), emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN)
    _run(interp)
    # The node error is caught by the run loop (falls through), so the call still ends
    # cleanly — but the summary must exist either way, which is the point of the `finally`.
    check("a summary was emitted", len(rec.summaries()) == 1)
    check("the failing node's exit is flagged errored", rec.exit_for("start")["errored"] is True)
    check("the failing node's port renders as 'error', never the raw sentinel",
          rec.exit_for("start")["port"] == "error")


def test_observability_emit_failure_never_breaks_a_call():
    print("an emit that FAILS on the new events cannot dead-air a call:")

    class FlakyRecorder(Recorder):
        async def emit(self, event_type, seq, payload):
            # The new observability events fail; the original transition stream succeeds.
            if event_type in ("flow.node.exit", "flow.call.summary"):
                raise RuntimeError("postgres is down")
            await super().emit(event_type, seq, payload)

    ari = FakeAri(digit="1", dial_result="answered")
    rec = FlakyRecorder()
    interp = FlowInterpreter(graph=_graph(), channel_id=CHAN, ari=ari, emit=rec.emit,
                             linkedid=LINKEDID, now=ALWAYS_OPEN)
    _run(interp)  # must not raise
    check("the call still ran end to end",
          rec.node_ids() == ["start", "hrs", "greet", "menu", "sales", "bye"])
    check("the caller was still connected", "dial" in ari.ops())


def main():
    test_happy_path_pins_and_emits_one_per_transition()
    test_exit_events_record_port_and_routing()
    test_menu_timeout_is_distinguishable_from_invalid_digit()
    test_call_summary_records_path_and_end_reason()
    test_summary_names_the_caller_dropped_case()
    test_truncated_path_keeps_step_count_and_terminal_node_true()
    test_summary_is_emitted_even_when_a_node_explodes()
    test_observability_emit_failure_never_breaks_a_call()
    test_menu_dtmf_routes_to_correct_port()
    test_unwired_digit_falls_to_default_fallback()
    test_errored_node_falls_to_default_fallback()
    test_hours_closed_routes_to_closed_port()
    test_missing_fallback_dead_end_hangs_up_cleanly()
    test_dial_busy_routes_to_busy_port()
    test_operator_target_individual_dials_operator()
    test_operator_target_group_dedups_and_routes()
    test_operator_target_no_answer_falls_to_default_fallback()
    test_operator_target_empty_falls_to_default_fallback()
    test_number_target_still_works()
    test_set_vars_interpolation_and_snapshot()
    test_unset_vars_removes_names()
    test_conditions_routing_and_gather_digits()
    test_send_sms_fire_and_forget()
    test_request_node_success_failure_and_dot_path()
    test_runtime_builtins_seeded()
    test_evaluate_hours_pure()
    print("\nALL FLOW INTERPRETER CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)

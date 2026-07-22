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

    async def dial_number(self, channel_id, number, *, caller_id, timeout_s):
        self.calls.append(("dial", channel_id, number))
        return self._dial_result

    async def hangup(self, channel_id):
        self.calls.append(("hangup", channel_id, None))

    def ops(self):
        return [c[0] for c in self.calls]


class Recorder:
    """Captures emitted call_events (one per transition) and on_start (the version pin)."""

    def __init__(self):
        self.events = []      # (event_type, provider_sequence, payload)
        self.started = 0

    async def emit(self, event_type, seq, payload):
        self.events.append((event_type, seq, payload))

    async def pin(self):
        # Records that the pin happened, and how many events preceded it (must be 0).
        self.started += 1
        self._events_before_pin = len(self.events)

    def types(self):
        return [e[0] for e in self.events]

    def node_ids(self):
        return [e[2]["flow"]["node_id"] for e in self.events]


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
    check("one event per transition (6)", len(rec.events) == 6)
    check("event types are flow.node.<type>",
          rec.types() == ["flow.node.entry", "flow.node.hours", "flow.node.play",
                          "flow.node.menu", "flow.node.dial", "flow.node.hangup"])
    check("dedup key is {linkedid}:{step}:{node_id}", rec.events[0][1] == f"{LINKEDID}:1:start")
    check("entry answered the channel", ari.ops()[0] == "answer")
    check("play node with record modifier recorded then played",
          ("record", CHAN, f"{LINKEDID}-play-1") in ari.calls and ("play", CHAN, "sound:welcome") in ari.calls)
    check("dial node with record modifier recorded before dialling", ("record", CHAN, f"{LINKEDID}-dial-2") in ari.calls)
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
    check("voicemail played greeting, recorded, and hung up",
          ari.ops()[-3:] == ["play", "record", "hangup"])


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


def test_evaluate_hours_pure():
    print("evaluate_hours (pure) — open window vs outside window vs no-schedule fail-open:")
    node = {"type": "hours", "hours": {"tz": "America/New_York", "schedule": {"wed": [["09:00", "17:00"]]}}}
    open_now = datetime(2026, 7, 22, 16, 0, tzinfo=timezone.utc)   # Wed 12:00 ET -> open
    closed_now = datetime(2026, 7, 22, 3, 0, tzinfo=timezone.utc)  # Tue 23:00 ET -> closed
    check("inside window -> open", evaluate_hours(node, open_now, "UTC") is True)
    check("outside window -> closed", evaluate_hours(node, closed_now, "UTC") is False)
    check("no schedule -> fail open", evaluate_hours({"type": "hours"}, closed_now, "UTC") is True)


def main():
    test_happy_path_pins_and_emits_one_per_transition()
    test_menu_dtmf_routes_to_correct_port()
    test_unwired_digit_falls_to_default_fallback()
    test_errored_node_falls_to_default_fallback()
    test_hours_closed_routes_to_closed_port()
    test_missing_fallback_dead_end_hangs_up_cleanly()
    test_dial_busy_routes_to_busy_port()
    test_evaluate_hours_pure()
    print("\nALL FLOW INTERPRETER CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)

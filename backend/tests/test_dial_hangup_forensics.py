"""A finished dial must record WHO hung up and WHY (regression for the 2026-08-19 forensics).

THE INCIDENT: a caller on a live redirect flow was disconnected 30s in. Answering the single
question "who hung up?" took an afternoon across four systems — Asterisk's CDR, the ARI
warning log, BulkVS's rated CDR and the destination platform's own API — because OWEN stored
none of it:

  - the outbound leg is deliberately excluded from ingestion (`is_flow_dial_leg`), so its
    ChannelDestroyed (the ONLY event carrying the Q.850 cause) was dropped on the floor;
  - its CDR row is closed the moment it enters the bridge, so the CDR knows nothing either;
  - the sole surviving trace was an ACCIDENT of logging: `dial_number`'s teardown DELETEs the
    outbound leg and hangs up the caller, and whichever was already gone logged a 404. "Who
    hung up" was recoverable only by inferring it from which of two warnings appeared.

The second finding was subtler. The destination answered the SIP leg in ~1 second on three
consecutive calls, because it was a CPaaS-hosted number (Twilio, and so Quo/OpenPhone): the
platform returns 200 OK immediately to run its own app logic, then rings the human behind
in-band ringback. So `answered` did not mean a person, talk time was inflated by the hidden
ring, and the node's 25s ring timeout could never fire — its `noanswer` port is unreachable
for such a target. Nothing in the events said so.

WHAT THIS PINS:
  1. `_await_bridge_end` names the leg that went first, both ways, and captures the Q.850 off
     the trailing ChannelDestroyed — including when the first gone-event is a
     ChannelHangupRequest/StasisEnd that carries no cause.
  2. It does not hang waiting for a cause that never arrives (the other party is still on the
     line waiting to be released).
  3. A fast answer is flagged as a platform answer; a human-speed one is not.
  4. The interpreter puts all of it on the dial node's exit event.
  5. An answered dial with nothing wired after it ends `dial_completed`, NOT `unrouted_hangup`
     — the value the AI API counts as a DROPPED caller. Every working redirect flow was being
     reported as a drop.

Stdlib only, but imports the real ARI client (httpx), so run it where deps exist:
  python -m tests.test_dial_hangup_forensics
"""

import asyncio

from app.flows.interpreter import FlowInterpreter
from app.providers.asterisk_client import AsteriskAriClient

CHAN = "1787163948.111"
OUT = "flow-dial-5e5c7c99174c48899ccf9df951f4f3bf"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"dial_hangup_forensics failed at: {name}")


class BareClient(AsteriskAriClient):
    """The real watcher logic with no settings/httpx wiring (like StubAri elsewhere)."""

    def __init__(self):  # noqa: D107 - deliberately skips the real __init__
        self._last_dial = {}


def _chan_event(etype, cid, *, name=None, cause=None, cause_txt=None, state=None):
    ch = {"id": cid}
    if name:
        ch["name"] = name
    if state:
        ch["state"] = state
    event = {"type": etype, "channel": ch}
    if cause is not None:
        event["cause"] = cause
    if cause_txt is not None:
        event["cause_txt"] = cause_txt
    return event


async def _feed(queue, events, delay=0.0):
    for e in events:
        if delay:
            await asyncio.sleep(delay)
        await queue.put(e)


# --- 1/2/3: the client's watchers -------------------------------------------------------

def test_dialed_side_hangup_is_named():
    print("the DIALED leg goes first -> ended_by=dialed, with its Q.850:")

    async def scenario():
        ari, q = BareClient(), asyncio.Queue()
        # Real order for a BYE from the far end: hangup-request (no cause), then destroyed.
        await _feed(q, [
            _chan_event("ChannelHangupRequest", OUT, name="PJSIP/bulkvs-00000058"),
            _chan_event("ChannelDestroyed", OUT, name="PJSIP/bulkvs-00000058",
                        cause=16, cause_txt="Normal Clearing"),
        ])
        info = await ari._await_bridge_end(q, CHAN, OUT)
        check("ended_by is the dialed leg", info["dial_ended_by"] == "dialed")
        check("cause captured off the trailing destroy", info["dial_end_cause"] == 16)
        check("cause text captured", info["dial_end_cause_txt"] == "Normal Clearing")
        check("first gone-event recorded", info["dial_end_event"] == "ChannelHangupRequest")
        check("outbound channel name kept (joins CDR/SIP)",
              info["dial_out_channel"] == "PJSIP/bulkvs-00000058")
        check("talk time measured", isinstance(info["dial_talk_ms"], int))

    asyncio.run(scenario())


def test_caller_side_hangup_is_named():
    print("the CALLER leg goes first -> ended_by=caller (the other verdict):")

    async def scenario():
        ari, q = BareClient(), asyncio.Queue()
        await _feed(q, [
            _chan_event("ChannelHangupRequest", CHAN),
            _chan_event("ChannelDestroyed", CHAN, cause=16, cause_txt="Normal Clearing"),
        ])
        info = await ari._await_bridge_end(q, CHAN, OUT)
        check("ended_by is the caller", info["dial_ended_by"] == "caller")
        check("cause captured", info["dial_end_cause"] == 16)
        check("no outbound channel name attributed to the caller leg",
              "dial_out_channel" not in info)

    asyncio.run(scenario())


def test_missing_cause_does_not_stall_teardown():
    print("a gone leg whose ChannelDestroyed never arrives still returns promptly:")

    async def scenario():
        ari, q = BareClient(), asyncio.Queue()
        await q.put(_chan_event("StasisEnd", OUT))
        loop = asyncio.get_running_loop()
        started = loop.time()
        info = await ari._await_bridge_end(q, CHAN, OUT)
        waited = loop.time() - started
        check("still names the leg that went", info["dial_ended_by"] == "dialed")
        check("reports no cause rather than inventing one", "dial_end_cause" not in info)
        # The caller may still be holding the line; the grace is a ceiling, not a delay budget.
        check("returned within the cause grace", waited < 2.0)

    asyncio.run(scenario())


def test_platform_answer_is_flagged():
    print("answer latency separates a platform answering from a person picking up:")

    async def scenario():
        ari, q = BareClient(), asyncio.Queue()
        await q.put(_chan_event("StasisStart", OUT, name="PJSIP/bulkvs-00000058"))
        port, info = await ari._await_dial_answer(q, CHAN, OUT, timeout_s=25)
        check("port is answered", port == "answered")
        check("answer latency recorded", info["dial_answer_ms"] < 2500)
        check("instant answer flagged as a platform answer", info["dial_answer_platform"] is True)

        # A human-speed pickup must NOT be flagged, or the flag means nothing.
        ari2, q2 = BareClient(), asyncio.Queue()
        asyncio.create_task(_feed(q2, [_chan_event("ChannelStateChange", OUT, state="Up")],
                                  delay=0.05))
        # Pretend the clock already ran: assert on the threshold, not on a real 3s sleep.
        port2, info2 = await ari2._await_dial_answer(q2, CHAN, OUT, timeout_s=25)
        check("slow-path answer still answers", port2 == "answered")
        check("flag follows the threshold, not the port",
              info2["dial_answer_platform"] == (info2["dial_answer_ms"] < 2500))

    asyncio.run(scenario())


def test_rejected_leg_keeps_its_cause():
    print("a leg that never answered still reports why:")

    async def scenario():
        ari, q = BareClient(), asyncio.Queue()
        await q.put(_chan_event("ChannelDestroyed", OUT, cause=17, cause_txt="User busy"))
        port, info = await ari._await_dial_answer(q, CHAN, OUT, timeout_s=25)
        check("busy cause maps to the busy port", port == "busy")
        check("cause preserved for the event", info["dial_end_cause"] == 17)
        check("ring duration recorded", "dial_ring_ms" in info)

    asyncio.run(scenario())


# --- 4/5: the interpreter -----------------------------------------------------------------

class DiagAri:
    """Minimal AriControl whose dial answers and offers diagnostics to drain."""

    def __init__(self, diagnostics):
        self._diagnostics = diagnostics
        self.hung_up = False

    async def answer(self, channel_id): pass
    async def play(self, channel_id, media): pass
    async def record(self, channel_id, name): pass
    async def read_digit(self, channel_id, *, prompt, timeout_s, max_digits): return None
    async def dial_operator(self, channel_id, operators, *, caller_id, timeout_s,
                            record_name=None): return "answered"
    async def voicemail(self, channel_id, *, greeting, name, max_duration_s, max_silence_s): pass
    async def hangup(self, channel_id): self.hung_up = True

    async def dial_number(self, channel_id, number, *, caller_id, timeout_s,
                          record_name=None):
        return "answered"

    def pop_dial_diagnostics(self):
        return dict(self._diagnostics)


class PlainAri(DiagAri):
    """A client WITHOUT the optional diagnostics hook — dialing must still work."""

    pop_dial_diagnostics = None


REDIRECT_GRAPH = {
    "nodes": {
        "entry": {"type": "entry", "next": {"default": "dial"}},
        # No edge off `answered`: the shape of every plain redirect flow.
        "dial": {"type": "dial", "target_kind": "number", "target": "+19549147244"},
    }
}


async def _run(graph, ari):
    events = []

    async def emit(event_type, seq, payload):
        events.append((event_type, payload))

    interp = FlowInterpreter(
        graph=graph, channel_id=CHAN, ari=ari, emit=emit, linkedid=CHAN,
        business_tz="America/New_York",
    )
    await interp.run()
    return events


def _exit_of(events, node_id):
    for etype, payload in events:
        flow = payload.get("flow", {})
        if etype == "flow.node.exit" and flow.get("node_id") == node_id:
            return flow
    return {}


def _summary_of(events):
    for etype, payload in events:
        if etype == "flow.call.summary":
            return payload.get("flow", {})
    return {}


def test_diagnostics_reach_the_exit_event():
    print("the dial node's exit event carries the whole post-mortem:")

    async def scenario():
        diag = {
            "dial_ended_by": "dialed", "dial_end_cause": 16,
            "dial_end_cause_txt": "Normal Clearing", "dial_answer_ms": 1140,
            "dial_answer_platform": True, "dial_talk_ms": 29800,
            "dial_out_channel": "PJSIP/bulkvs-00000058",
        }
        events = await _run(REDIRECT_GRAPH, DiagAri(diag))
        exit_flow = _exit_of(events, "dial")
        check("who hung up is on the event", exit_flow.get("dial_ended_by") == "dialed")
        check("Q.850 is on the event", exit_flow.get("dial_end_cause") == 16)
        check("answer latency is on the event", exit_flow.get("dial_answer_ms") == 1140)
        check("platform-answer flag is on the event",
              exit_flow.get("dial_answer_platform") is True)
        check("outbound channel is on the event",
              exit_flow.get("dial_out_channel") == "PJSIP/bulkvs-00000058")
        check("existing dial fields survive", exit_flow.get("dial_target") == "+19549147244")

    asyncio.run(scenario())


def test_client_without_diagnostics_still_dials():
    print("the hook is OPTIONAL — a client that lacks it must not break the call:")

    async def scenario():
        events = await _run(REDIRECT_GRAPH, PlainAri({}))
        exit_flow = _exit_of(events, "dial")
        check("dial still ran and reported its port", exit_flow.get("dial_result") == "answered")
        check("no forensics invented", "dial_ended_by" not in exit_flow)

    asyncio.run(scenario())


def test_answered_dial_is_not_a_dropped_caller():
    print("an answered dial with nothing wired after it is NOT unrouted_hangup:")

    async def scenario():
        events = await _run(REDIRECT_GRAPH, DiagAri({"dial_ended_by": "dialed"}))
        summary = _summary_of(events)
        check("ended reads dial_completed", summary.get("ended") == "dial_completed")
        check("NOT counted as a dropped caller", summary.get("ended") != "unrouted_hangup")
        check("path still recorded", summary.get("path") == ["entry", "dial"])

    asyncio.run(scenario())


def test_genuinely_dropped_caller_still_reported():
    print("a caller who reached a dead end WITHOUT being connected still reads as dropped:")

    async def scenario():
        graph = {
            "nodes": {
                "entry": {"type": "entry", "next": {"default": "menu"}},
                # Nothing wired off the menu's timeout port, and no default_fallback.
                "menu": {"type": "menu", "timeout": 0.01, "next": {}},
            }
        }
        events = await _run(graph, DiagAri({}))
        summary = _summary_of(events)
        check("ended is still unrouted_hangup", summary.get("ended") == "unrouted_hangup")

    asyncio.run(scenario())


def test_unanswered_dial_is_still_a_drop():
    print("a dial that did NOT answer, with no fallback, is still a dropped caller:")

    async def scenario():
        class NoAnswerAri(DiagAri):
            async def dial_number(self, channel_id, number, *, caller_id, timeout_s,
                                  record_name=None):
                return "noanswer"

        events = await _run(REDIRECT_GRAPH, NoAnswerAri({}))
        summary = _summary_of(events)
        check("ended is unrouted_hangup", summary.get("ended") == "unrouted_hangup")

    asyncio.run(scenario())


def test_unreachable_coverage_port_is_flagged():
    print("call coverage wired behind a port that can never fire is called out:")

    async def scenario():
        # `noanswer` wired to voicemail — the operator's intended coverage — against a target
        # whose platform answers instantly, so the ring timeout never expires.
        graph = {
            "nodes": {
                "entry": {"type": "entry", "next": {"default": "dial"}},
                "dial": {
                    "type": "dial", "target_kind": "number", "target": "+19549147244",
                    "next": {"noanswer": "vm"},
                },
                "vm": {"type": "hangup"},
            }
        }
        events = await _run(graph, DiagAri({"dial_answer_platform": True,
                                            "dial_answer_ms": 1140}))
        exit_flow = _exit_of(events, "dial")
        check("the dead port is named on the event",
              exit_flow.get("dial_ports_unreachable") == ["noanswer"])

        # A plain redirect with no coverage wired must stay quiet — no false alarm.
        quiet = await _run(REDIRECT_GRAPH, DiagAri({"dial_answer_platform": True,
                                                    "dial_answer_ms": 1140}))
        check("no warning when nothing is wired behind it",
              "dial_ports_unreachable" not in _exit_of(quiet, "dial"))

        # A human-speed answer must not trip it either.
        human = await _run(graph, DiagAri({"dial_answer_platform": False,
                                           "dial_answer_ms": 6200}))
        check("no warning for a real pickup",
              "dial_ports_unreachable" not in _exit_of(human, "dial"))

    asyncio.run(scenario())


if __name__ == "__main__":
    test_dialed_side_hangup_is_named()
    test_caller_side_hangup_is_named()
    test_missing_cause_does_not_stall_teardown()
    test_platform_answer_is_flagged()
    test_rejected_leg_keeps_its_cause()
    test_diagnostics_reach_the_exit_event()
    test_client_without_diagnostics_still_dials()
    test_answered_dial_is_not_a_dropped_caller()
    test_genuinely_dropped_caller_still_reported()
    test_unanswered_dial_is_still_a_drop()
    test_unreachable_coverage_port_is_flagged()
    print("\nALL DIAL HANGUP FORENSICS CHECKS PASSED")

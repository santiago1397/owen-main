"""Unit test for the per-channel ARI event registries (app.flows.dtmf — Ticket 15.1/15.3).

Dependency-free (stdlib asyncio only, like test_flow_interpreter): exercises the DTMF digit
queues and the channel lifecycle watchers the WS consumer feeds and the ARI client awaits.

Asserts:
- register/push/await round-trips a digit; multiple digits arrive in order (max_digits);
- pushing to an unregistered channel drops (returns False) — never raises, never leaks;
- unregister_digits removes the queue (and is idempotent) — the registry never leaks;
- a full digit queue drops the overflow digit without blocking the pusher;
- watch() fans one channel's events to the watcher; one queue can watch BOTH call legs;
- unwatch detaches (idempotent, tolerates never-watched ids) and empties the registry;
- push_channel_event to an unwatched channel is a cheap no-op returning 0.

Run: python -m tests.test_flow_dtmf
"""

import asyncio
import sys

from app.flows import dtmf


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"flow_dtmf failed at: {name}")


CHAN = "1690000000.1"
OUT = "flow-dial-abc123"


def test_digit_round_trip():
    print("digit queue — register, push, await (in order), unregister:")

    async def scenario():
        queue = dtmf.register_digits(CHAN)
        check("registered queue is retrievable", dtmf.digit_queue(CHAN) is queue)
        check("push to registered channel accepted", dtmf.push_digit(CHAN, "1") is True)
        check("second digit accepted", dtmf.push_digit(CHAN, "2") is True)
        first = await asyncio.wait_for(queue.get(), timeout=1)
        second = await asyncio.wait_for(queue.get(), timeout=1)
        check("digits arrive in press order", (first, second) == ("1", "2"))
        dtmf.unregister_digits(CHAN)
        check("unregistered channel drops digits", dtmf.push_digit(CHAN, "3") is False)
        dtmf.unregister_digits(CHAN)  # idempotent
        check("registry empty after unregister", dtmf.registry_sizes() == (0, 0))

    asyncio.run(scenario())


def test_digit_push_without_registration_drops():
    print("push to a never-registered channel is a dropped no-op:")
    check("push returns False", dtmf.push_digit("ghost-channel", "5") is False)
    check("empty digit is dropped even when registered",
          (dtmf.register_digits(CHAN), dtmf.push_digit(CHAN, ""))[1] is False)
    dtmf.unregister_digits(CHAN)
    check("no leak", dtmf.registry_sizes() == (0, 0))


def test_digit_queue_overflow_drops():
    print("a full digit queue drops overflow without blocking:")
    dtmf.register_digits(CHAN)
    accepted = [dtmf.push_digit(CHAN, "1") for _ in range(dtmf._DIGIT_QUEUE_MAX + 5)]
    check("first pushes accepted", all(accepted[: dtmf._DIGIT_QUEUE_MAX]))
    check("overflow pushes dropped (False)", not any(accepted[dtmf._DIGIT_QUEUE_MAX:]))
    dtmf.unregister_digits(CHAN)
    check("no leak", dtmf.registry_sizes() == (0, 0))


def test_watchers_fan_out_both_legs():
    print("channel watchers — one queue watches BOTH call legs (the dial pattern):")

    async def scenario():
        queue = dtmf.watch(OUT, CHAN)
        evt_out = {"type": "StasisStart", "channel": {"id": OUT}}
        evt_in = {"type": "ChannelHangupRequest", "channel": {"id": CHAN}}
        check("out-leg event delivered to 1 watcher", dtmf.push_channel_event(OUT, evt_out) == 1)
        check("in-leg event delivered to the SAME queue", dtmf.push_channel_event(CHAN, evt_in) == 1)
        got1 = await asyncio.wait_for(queue.get(), timeout=1)
        got2 = await asyncio.wait_for(queue.get(), timeout=1)
        check("events received in push order", (got1, got2) == (evt_out, evt_in))
        check("unwatched channel -> 0 deliveries", dtmf.push_channel_event("other", evt_out) == 0)
        dtmf.unwatch(queue, OUT, CHAN)
        check("push after unwatch -> 0 deliveries", dtmf.push_channel_event(OUT, evt_out) == 0)
        dtmf.unwatch(queue, OUT, CHAN, "never-watched")  # idempotent + tolerant
        check("registry empty after unwatch", dtmf.registry_sizes() == (0, 0))

    asyncio.run(scenario())


def test_multiple_watchers_per_channel():
    print("two independent watchers on one channel both receive the event:")
    q1 = dtmf.watch(CHAN)
    q2 = dtmf.watch(CHAN)
    evt = {"type": "ChannelDestroyed", "channel": {"id": CHAN}, "cause": 16}
    check("delivered to both", dtmf.push_channel_event(CHAN, evt) == 2)
    check("both queues hold it", q1.get_nowait() == evt and q2.get_nowait() == evt)
    dtmf.unwatch(q1, CHAN)
    check("remaining watcher still fed", dtmf.push_channel_event(CHAN, evt) == 1)
    dtmf.unwatch(q2, CHAN)
    check("registry empty", dtmf.registry_sizes() == (0, 0))


def main():
    test_digit_round_trip()
    test_digit_push_without_registration_drops()
    test_digit_queue_overflow_drops()
    test_watchers_fan_out_both_legs()
    test_multiple_watchers_per_channel()
    print("\nALL FLOW DTMF REGISTRY CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)

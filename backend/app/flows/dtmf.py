"""Per-channel ARI event registries (Ticket 15.1/15.3) — the WS↔interpreter correlation seam.

The ARI events WebSocket consumer (app/workers/asterisk_consumer.py) is the ONLY reader of
Asterisk's event stream, but two interpreter operations need to OBSERVE events mid-call:

- `read_digit` must see `ChannelDtmfReceived` for the caller's channel (Ticket 15.1);
- `dial_number` must see the dialed leg's lifecycle (`StasisStart` / `ChannelStateChange` /
  `ChannelHangupRequest` / `StasisEnd` / `ChannelDestroyed`) to confirm answered/busy/... and
  to notice either leg leaving a bridge (Ticket 15.3).

This module is the hand-off: per-channel `asyncio.Queue` registries the consumer FEEDS
(`push_digit` / `push_channel_event`) and the ARI control client AWAITS on. Two registries:

- DIGIT queues: one per live flow-run entry channel. The consumer registers the channel
  around a flow run (register in `_run_flow`'s try, unregister in its finally — never leaks)
  and `AsteriskAriClient.read_digit` awaits the queue with the node's timeout.
- CHANNEL-EVENT watchers: transient, created by `dial_number` around one dial attempt
  (`watch(...)` before originate, `unwatch(...)` in a finally). One queue may watch several
  channels (both call legs) so a single await sees whichever leg moves first.

Everything here is stdlib-only (mirrors app/flows/interpreter.py) so it is unit-testable in
the sandbox, and every push is non-blocking + bounded: a slow/stuck consumer of a queue can
never back-pressure the WS read loop (overflow drops the event — DTMF/lifecycle correlation
is best-effort; the flow's fallback semantics absorb a miss).
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("flows.dtmf")

# Bounded queues so an unread registry entry can never grow without limit.
_DIGIT_QUEUE_MAX = 64
_EVENT_QUEUE_MAX = 256

# ARI event types the consumer forwards to channel-event watchers (the dialed-leg lifecycle
# `dial_number` correlates on). Kept here so the consumer and the client share one source.
CHANNEL_EVENT_TYPES: frozenset[str] = frozenset(
    {"StasisStart", "StasisEnd", "ChannelStateChange", "ChannelHangupRequest", "ChannelDestroyed"}
)

# channel_id -> queue of pressed digit strings (one entry per live flow run).
_digit_queues: dict[str, asyncio.Queue] = {}
# channel_id -> queues watching that channel's lifecycle events (transient, per dial).
_watchers: dict[str, list[asyncio.Queue]] = {}


# --- DTMF digit queues (Ticket 15.1) ------------------------------------------------------

def register_digits(channel_id: str) -> asyncio.Queue:
    """Create (or replace) the digit queue for a channel. The consumer calls this right
    before running the flow interpreter on the channel's StasisStart."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=_DIGIT_QUEUE_MAX)
    _digit_queues[str(channel_id)] = queue
    return queue


def unregister_digits(channel_id: str) -> None:
    """Drop the channel's digit queue. MUST run in the flow-run's finally so a crashed or
    finished flow never leaks a queue."""
    _digit_queues.pop(str(channel_id), None)


def digit_queue(channel_id: str) -> asyncio.Queue | None:
    """The channel's digit queue, or None when no flow run registered it (e.g. the REST
    client is driven outside the consumer — read_digit then times out via the node port)."""
    return _digit_queues.get(str(channel_id))


def push_digit(channel_id: str, digit: str) -> bool:
    """Feed one ChannelDtmfReceived digit to the channel's waiting reader. Non-blocking;
    returns False when nothing is registered (dropped) or the queue is full."""
    queue = _digit_queues.get(str(channel_id))
    if queue is None or not digit:
        return False
    try:
        queue.put_nowait(str(digit))
        return True
    except asyncio.QueueFull:
        logger.warning("dtmf: digit queue full for channel %s; dropping digit", channel_id)
        return False


# --- Channel lifecycle watchers (Ticket 15.3) ---------------------------------------------

def watch(*channel_ids: str) -> asyncio.Queue:
    """Create one queue subscribed to the lifecycle events of every given channel id (both
    call legs of a dial). Pair with `unwatch` in a finally — watchers are transient."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=_EVENT_QUEUE_MAX)
    for cid in channel_ids:
        if cid:
            _watchers.setdefault(str(cid), []).append(queue)
    return queue


def unwatch(queue: asyncio.Queue, *channel_ids: str) -> None:
    """Detach `queue` from the given channel ids (idempotent — safe on ids it never watched)."""
    for cid in channel_ids:
        queues = _watchers.get(str(cid))
        if not queues:
            continue
        try:
            queues.remove(queue)
        except ValueError:
            pass
        if not queues:
            _watchers.pop(str(cid), None)


def push_channel_event(channel_id: str, event: dict) -> int:
    """Fan one ARI channel event out to every watcher of that channel. Non-blocking;
    returns how many watchers received it (0 = nobody cared — the common case)."""
    delivered = 0
    for queue in list(_watchers.get(str(channel_id), ())):
        try:
            queue.put_nowait(event)
            delivered += 1
        except asyncio.QueueFull:
            logger.warning("dtmf: watcher queue full for channel %s; dropping event", channel_id)
    return delivered


def registry_sizes() -> tuple[int, int]:
    """(digit queues, watched channels) currently registered — for tests/leak assertions."""
    return len(_digit_queues), len(_watchers)

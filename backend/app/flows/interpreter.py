"""In-memory ARI flow interpreter (Ticket 07).

Executes a call-flow-version graph against a single live inbound channel: the caller hears
the greeting, IVR routing works, calls forward, and voicemail catches the rest — never dead
air. One `FlowInterpreter` instance runs one call; interpreter state is entirely in-memory
(a worker restart drops the RTP/call anyway, so there is no persisted cursor).

DESIGN (mirrors app/flows/validator.py — dependency-light, unit-testable in isolation):
- This module imports ONLY stdlib. No sqlalchemy / httpx / websockets, so the interpreter
  core can be exercised with a FAKE ARI client and a fake emit() in the sandbox. The
  DB-aware glue (number->flow_version resolution, version pinning, call_event writes) lives
  in app/flows/runtime.py, and the concrete httpx ARI client lives in
  app/providers/asterisk_client.py — both behind the thin `AriControl` interface below.
- The graph shape is the one app/flows/validator.py validates:
    { "default_fallback": <node-id>, "nodes": { <id>: {"type", "next": {<port>: <id>}, ...} } }
  `record` is a MODIFIER flag on a node, never its own node type.
- Each node ENTERED emits exactly ONE call_event (via the injected `emit`), feeding the same
  event-sourced projection as ticket 04/05 — keyed on provider_call_sid = Linkedid.
- Unwired / errored ports fall through to the flow-level `default_fallback` (usually
  voicemail) so a call never hits dead air. If `default_fallback` is itself missing, the
  interpreter hangs up cleanly rather than leaving dead air.

SCOPE: the recordings pipeline is a LATER ticket. `ai_agent` runs a VoiceAgentSession through
the injected `run_agent` seam (Ticket 11) and exits by the returned port; with no seam injected
it keeps its legacy stub (routes to `default`). `dial` supports a NUMBER target and (Ticket 13)
an OPERATOR target (individual or group; via `dial_operator`). `record` merely drives ARI
record — the WAV fetch/transcribe reuse is ticket 05's job.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, ClassVar, Optional, Protocol
from zoneinfo import ZoneInfo

from app.flows.variables import evaluate_conditions, interpolate

logger = logging.getLogger("flows.interpreter")

# Node types that TERMINATE the call: once run, the interpreter stops (no onward routing).
TERMINAL_TYPES: frozenset[str] = frozenset({"voicemail", "hangup"})

# A node returning this port means a HUMAN has seized the call (AI_AGENT_SPEC D4). It is
# internal control, never a wireable graph edge (D5): the interpreter returns immediately and
# touches NOTHING — no routing, no fallback, no hangup.
#
# Without this, an agent session that ends because a supervisor took over falls through the
# normal path: the port is unwired, so it routes to `default_fallback` and plays a voicemail
# greeting at a caller who is mid-sentence with a human operator — or, with no fallback
# configured, `_safe_hangup()` hangs up on the caller the operator just rescued.
PORT_TAKEN_OVER: str = "taken_over"

# Ticket 17 parity nodes emit their transition event AFTER the handler runs (instead of the
# usual emit-on-entry), so the payload can snapshot the OUTCOME (vars set, matched condition
# row, request status). All of these run in bounded time (send_sms is fire-and-forget; the
# request node has a hard timeout), so deferring the emit never delays it meaningfully.
_POST_EMIT_TYPES: frozenset[str] = frozenset(
    {"set_vars", "unset_vars", "conditions", "send_sms", "request"}
)

# Event-payload snapshot cap: variable VALUES in flow.node.* payloads are truncated to this.
_SNAP_MAX = 200

# Longest node path recorded in a flow.call.summary payload. `max_steps` allows 100 hops, and
# a summary is a diagnostic, not an audit log — the head of the path is what explains an
# outcome, so a runaway loop truncates rather than writing a 100-element array per call.
_PATH_MAX = 50

# Sentinel port meaning "the handler could not choose a valid port" (unknown node type or a
# handler error). It never matches a wired edge, so it always falls through to the fallback.
_ERROR: str = "\x00__error__"

# Weekday index (Mon=0) -> the schedule key an `hours` node uses.
_DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Voicemail record caps (Ticket 18) when a `voicemail` node doesn't override them. The pure
# interpreter can't import settings; the runtime default handler passes settings values directly.
_VM_MAX_DURATION_S = 120.0
_VM_MAX_SILENCE_S = 5.0


def _snap(value: Any) -> str:
    """A value as it appears in a flow.node.* event payload: str()'d, capped at _SNAP_MAX."""
    return "" if value is None else str(value)[:_SNAP_MAX]


def _port_label(port: Optional[str]) -> Optional[str]:
    """The port as it appears in an event payload. The `_ERROR` sentinel is a control value
    containing a NUL byte — Postgres rejects NUL in jsonb text, so it is never written raw."""
    if port is None:
        return None
    return "error" if port == _ERROR else str(port)


# --- Injected collaborators (all substitutable with fakes in tests) -----------------------

class AriControl(Protocol):
    """Thin async interface over the ARI control operations the interpreter drives.

    The real implementation (httpx REST against ARI) is AsteriskAriClient in
    app/providers/asterisk_client.py; tests pass a FAKE implementing just these methods.
    `dial_number` returns one of the `dial` node's ports: "answered"|"noanswer"|"busy"|"failed".
    `read_digit` returns the pressed digit string, or None on timeout/no input.
    """

    async def answer(self, channel_id: str) -> None: ...
    async def play(self, channel_id: str, media: str) -> None: ...
    # Blocking playback: returns only once the prompt has FINISHED (the client correlates the
    # WS PlaybackFinished). OPTIONAL on this protocol — the minimum surface is the
    # fire-and-forget `play` above, and `_play_to_completion` falls back to it when a client
    # (or a test fake) doesn't implement this.
    async def play_and_wait(
        self, channel_id: str, media: str, *, timeout_s: float = 30.0
    ) -> None: ...
    async def record(self, channel_id: str, name: str) -> None: ...
    async def read_digit(
        self, channel_id: str, *, prompt: Optional[str], timeout_s: float, max_digits: int
    ) -> Optional[str]: ...
    # `record_name`, when set, records the BRIDGE once both legs are joined — NOT the caller's
    # channel. Recording a channel BEFORE bridging makes Asterisk refuse the bridge outright
    # ("Channel <id> currently recording", HTTP 409), which is exactly how a forwarded call
    # silently became 25s of dead air; see `_h_dial`.
    async def dial_number(
        self, channel_id: str, number: str, *, caller_id: Optional[str], timeout_s: float,
        record_name: Optional[str] = None,
    ) -> str: ...
    async def dial_operator(
        self, channel_id: str, operators: list, *, caller_id: Optional[str], timeout_s: float,
        record_name: Optional[str] = None,
    ) -> str: ...
    async def voicemail(
        self, channel_id: str, *, greeting: Optional[str], name: str,
        max_duration_s: float, max_silence_s: float,
    ) -> None: ...
    async def hangup(self, channel_id: str) -> None: ...
    # Post-mortem of the dial that just returned: which leg hung up first, its Q.850 cause,
    # how fast the far end answered. OPTIONAL on this protocol — a client that omits it dials
    # exactly the same, its dial events just carry no forensics (see `_dial_diagnostics`).
    def pop_dial_diagnostics(self) -> dict: ...


# emit(event_type, provider_sequence, payload) -> awaitable. One call per node transition.
EmitFn = Callable[[str, str, dict], Awaitable[None]]
# now() -> aware datetime. Injectable so `hours` evaluation is deterministic in tests.
ClockFn = Callable[[], datetime]
# on_start() -> awaitable. Runs ONCE at StasisStart before the first node — the seam where
# runtime pins the flow_version_id onto the call. Injectable so pinning is unit-testable.
StartFn = Callable[[], Awaitable[None]]
# run_agent(node) -> awaitable (port, data). The seam for the `ai_agent` node (Ticket 11):
# runtime resolves+pins the node's agent_version, runs a VoiceAgentSession, and returns the
# exit PORT ("transfer"|"end_call"|"default"|"failed") + any tool data. The interpreter drives
# the graph edge for that port — the agent NEVER bridges. Injectable so the node is unit-
# testable with a fake; when None the node keeps its legacy stub (routes to `default`).
RunAgentFn = Callable[[dict], Awaitable[tuple[str, dict]]]
# send_sms(to, body) -> awaitable bool. The seam for the `send_sms` node (Ticket 17): the
# runtime SCHEDULES the send through the platform outbound SMS service (opt-out + 10DLC
# gates apply) and returns immediately — fire-and-forget, the flow never waits on carriers.
# With no seam injected the node logs and continues (port `default` regardless).
SendSmsFn = Callable[[str, str], Awaitable[bool]]
# http_request(method, url, headers, body) -> awaitable (status, parsed_body). The seam for
# the `request` node (Ticket 17): the runtime performs the HTTP call (httpx, 5s hard
# timeout) so the interpreter stays transport-free. Transport errors/timeouts -> (0, None).
HttpRequestFn = Callable[[str, str, dict, Any], Awaitable[tuple[int, Any]]]


def _default_now() -> datetime:
    return datetime.now(timezone.utc)


def _operator_list(node: dict) -> list:
    """The operator id(s) an operator-target `dial` node reaches, as a de-duplicated list.

    Accepts an individual (`operator`) or a group (`operators`/`group` list). Group members
    may be plain ids or {"id": ...} objects (the flow-builder shape). Blanks are dropped.
    Pure/stdlib so it stays unit-testable with the interpreter core."""
    raw = node.get("operators")
    if raw is None:
        raw = node.get("group")
    if raw is None:
        single = node.get("operator") or node.get("target")
        raw = [single] if single else []
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    out: list = []
    for item in raw:
        op = item.get("id") if isinstance(item, dict) else item
        if op:
            op = str(op)
            if op not in out:
                out.append(op)
    return out


# --- Pure business-hours evaluation -------------------------------------------------------

def _to_minutes(hhmm: str) -> int:
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def evaluate_hours(node: dict, now: datetime, default_tz: str) -> bool:
    """Return True if the `hours` node is OPEN at `now` per its inline business-hours config.

    Pure. Config lives on the node (there is no separate business-hours table):
        {"type": "hours",
         "hours": {"tz": "America/New_York",
                   "schedule": {"mon": [["09:00","17:00"]], ...}},
         "next": {"open": ..., "closed": ...}}
    `tz` defaults to `default_tz` (settings.BUSINESS_TZ). With NO schedule configured we
    FAIL OPEN (route to the greeting) — better than sending every call to voicemail.
    """
    cfg = node.get("hours") or node.get("business_hours") or {}
    if not isinstance(cfg, dict):
        return True
    tz_name = cfg.get("tz") or node.get("tz") or default_tz
    try:
        local = now.astimezone(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001 - unknown tz -> evaluate in the given clock's zone
        local = now
    schedule = cfg.get("schedule") or cfg.get("weekly") or {}
    if not isinstance(schedule, dict) or not schedule:
        return True  # fail open
    windows = schedule.get(_DOW[local.weekday()]) or []
    cur = local.hour * 60 + local.minute
    for w in windows:
        try:
            if _to_minutes(w[0]) <= cur < _to_minutes(w[1]):
                return True
        except (ValueError, IndexError, TypeError):
            continue
    return False


# --- The interpreter ----------------------------------------------------------------------

@dataclass
class FlowInterpreter:
    """Runs ONE flow-version graph against ONE live channel. Construct per StasisStart.

    `linkedid` is the call's Linkedid (== provider_call_sid); it namespaces every emitted
    event's dedup key. `max_steps` caps pathological loops (a self-referential fallback):
    on hitting it the interpreter hangs up cleanly.
    """

    graph: dict
    channel_id: str
    ari: AriControl
    emit: EmitFn
    linkedid: str
    now: ClockFn = _default_now
    # Monotonic source for node/flow DURATIONS only (never wall-clock decisions — `now` stays
    # the clock `hours` evaluates against). Injectable so tests can assert exact `ms` values.
    monotonic: Callable[[], float] = time.monotonic
    business_tz: str = "America/New_York"
    max_steps: int = 100
    on_start: Optional[StartFn] = None
    run_agent: Optional[RunAgentFn] = None
    send_sms: Optional[SendSmsFn] = None
    http_request: Optional[HttpRequestFn] = None
    # Per-call variable store (Ticket 17). The runtime seeds the built-ins (caller_number,
    # dialed_number, call.time, call.dow) at construction; node handlers add gather.digits /
    # request.status / request.body / set_vars entries as the call progresses.
    variables: dict = field(default_factory=dict)
    _rec_counter: int = field(default=0, init=False)
    # Outcome snapshot the node handlers stash for their transition + exit events.
    _event_extra: Optional[dict] = field(default=None, init=False)
    # Node ids in the order they were entered, for the end-of-call flow.call.summary payload.
    # `_steps` is counted separately because `_path` is capped at _PATH_MAX: on a runaway loop
    # the path truncates but the step COUNT must stay true, or the summary would report a
    # 100-hop loop as a tidy 50-step call.
    _path: list = field(default_factory=list, init=False)
    _steps: int = field(default=0, init=False)
    # Tracked separately from `_path[-1]` for the same reason: once the path truncates, its
    # last element is the 50th node, not the one the call actually ended on.
    _last_node: Optional[str] = field(default=None, init=False)

    async def run(self) -> None:
        """Run the graph, then ALWAYS emit one `flow.call.summary`.

        The summary is the per-call answer to "what happened and why did it end that way" —
        without it, reconstructing an outcome means a window function over `flow.node.*`
        events plus a read of the version's graph JSON to learn what each port meant. The
        real work is in `_run_graph`, which returns the end reason; this wrapper exists so
        every one of its exit paths is summarized without repeating the emit at each return.
        """
        started = self.monotonic()
        reason = "error"
        try:
            reason = await self._run_graph()
        finally:
            await self._emit_summary(reason, int((self.monotonic() - started) * 1000))

    async def _run_graph(self) -> str:
        """Execute the graph; return the reason the flow ended (see `run`)."""
        # Pin the flow_version onto the call FIRST, at StasisStart, before any node runs
        # (mirrors campaign_id pinning at ingest). Best-effort: a pin failure must not
        # dead-air the caller, so we log and still run the flow.
        if self.on_start is not None:
            try:
                await self.on_start()
            except Exception:  # noqa: BLE001
                logger.exception("interpreter %s: on_start (version pin) failed", self.linkedid)

        nodes = self.graph.get("nodes")
        if not isinstance(nodes, dict) or not nodes:
            logger.warning("interpreter %s: graph has no nodes; hanging up", self.linkedid)
            await self._safe_hangup()
            return "empty_graph"

        fallback = self.graph.get("default_fallback")
        fallback = fallback if isinstance(fallback, str) and fallback in nodes else None
        if fallback is None:
            # Worth a line at INFO: with no fallback, EVERY unwired or errored port hangs up on
            # the caller instead of routing to voicemail. That is a flow-authoring decision
            # made (or forgotten) at design time, and it is invisible in a per-node event.
            logger.info(
                "interpreter %s: no default_fallback — unwired/errored ports will hang up",
                self.linkedid,
            )

        current: Optional[str] = self._entry_id(nodes)
        if current is None:
            logger.warning("interpreter %s: graph has no entry node; hanging up", self.linkedid)
            await self._safe_hangup()
            return "no_entry"

        step = 0
        while current is not None:
            if step >= self.max_steps:
                logger.warning("interpreter %s exceeded max_steps; hanging up", self.linkedid)
                await self._safe_hangup()
                return "max_steps"
            step += 1

            node = nodes.get(current)
            if not isinstance(node, dict):
                # Dangling target: fall to fallback once, else hang up.
                logger.warning(
                    "interpreter %s: edge points at missing node '%s'", self.linkedid, current
                )
                current, fallback = self._fall(fallback)
                if current is None:
                    await self._safe_hangup()
                    return "dangling_edge"
                continue

            ntype = node.get("type")
            self._steps += 1
            self._last_node = current
            if len(self._path) < _PATH_MAX:
                self._path.append(current)
            # Ticket 17 parity nodes emit AFTER the handler so the event snapshots the
            # outcome; everything else keeps the original emit-on-entry.
            post_emit = ntype in _POST_EMIT_TYPES
            if not post_emit:
                await self._emit_transition(step, current, ntype)

            self._event_extra = None
            node_started = self.monotonic()
            errored = False
            try:
                port = await self._run_node(node, ntype)
            except Exception:  # noqa: BLE001 - a node failure must fall through, not dead-air
                logger.exception("interpreter %s: node '%s' (%s) failed", self.linkedid, current, ntype)
                port = _ERROR
                errored = True
            node_ms = int((self.monotonic() - node_started) * 1000)

            if post_emit:
                await self._emit_transition(step, current, ntype, extra=self._event_extra)

            if port == PORT_TAKEN_OVER:
                # A human owns this call now. Record it and stand down — no routing, no
                # fallback, no hangup. See PORT_TAKEN_OVER.
                await self._emit_exit(
                    step, current, ntype, port, "taken_over", None, node_ms, errored
                )
                logger.info("interpreter %s: call taken over by a human; standing down",
                            self.linkedid)
                return "taken_over"

            if ntype in TERMINAL_TYPES:
                await self._emit_exit(step, current, ntype, port, "terminal", None, node_ms, errored)
                return "terminal"  # voicemail / hangup already terminated the channel

            nxt = self._resolve(node, port)
            if nxt is not None:
                await self._emit_exit(step, current, ntype, port, "edge", nxt, node_ms, errored)
                current = nxt
            else:
                # Unwired or errored port -> the flow-level fallback (once), else clean hangup.
                routed = "fallback" if fallback is not None else "hangup"
                await self._emit_exit(
                    step, current, ntype, port, routed, fallback, node_ms, errored
                )
                current, fallback = self._fall(fallback)
                if current is None:
                    await self._safe_hangup()
                    # A dial that ANSWERED already served the caller: the two legs were
                    # bridged and talked, and the flow simply has nothing wired after it —
                    # which is the normal shape of a plain redirect flow. That is NOT a
                    # dropped caller, and counting it as one made every WORKING forward look
                    # like a drop in /api/ai/flows (`dropped` = calls ended unrouted_hangup),
                    # burying the real ones. `unrouted_hangup` keeps its meaning: the caller
                    # hit a dead end WITHOUT being connected to anything.
                    if ntype == "dial" and port == "answered":
                        return "dial_completed"
                    return "unrouted_hangup"

        await self._safe_hangup()
        return "completed"

    # --- routing helpers ---

    @staticmethod
    def _entry_id(nodes: dict) -> Optional[str]:
        for nid, n in nodes.items():
            if isinstance(n, dict) and n.get("type") == "entry":
                return nid
        return None

    @staticmethod
    def _resolve(node: dict, port: Optional[str]) -> Optional[str]:
        """The wired target for `port`, or None (caller falls through to the fallback)."""
        edges = node.get("next")
        if not isinstance(edges, dict) or port is None:
            return None
        return edges.get(port)

    @staticmethod
    def _fall(fallback: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """Consume the one-shot fallback: return (next_node, remaining_fallback).

        Fallback is one-shot so a fallback node that itself has an unwired port can't spin
        the interpreter — the second miss hangs up cleanly instead of looping."""
        return fallback, None

    # --- node handlers (return the chosen PORT; terminal nodes return None) ---

    async def _run_node(self, node: dict, ntype: Optional[str]) -> Optional[str]:
        handler = self._HANDLERS.get(ntype or "")
        if handler is None:
            return _ERROR  # unknown node type -> fall through to fallback
        return await handler(self, node)

    async def _h_entry(self, node: dict) -> Optional[str]:
        await self.ari.answer(self.channel_id)
        return "default"

    async def _h_play(self, node: dict) -> Optional[str]:
        if node.get("record"):
            await self.ari.record(self.channel_id, self._rec_name("play"))
        media = self._interp(self._media(node))
        if media:
            await self._play_to_completion(media)
        # `played` distinguishes a prompt that ran from a node that resolved to no media at
        # all (an empty label / unresolved {{var}}) — the difference between a consent notice
        # the caller heard and one that silently never played. Paired with the exit event's
        # `ms`, a near-zero duration on a played prompt means the audio was never rendered.
        self._event_extra = {"played": bool(media), "media": _snap(media) if media else None}
        return "default"

    async def _play_to_completion(self, media: str) -> None:
        """Play a `play` node's prompt and BLOCK until it has finished.

        Returning early here is not cosmetic. The overwhelmingly common use of a `play` node is
        a recording-consent notice sitting immediately before a `dial`, and the fire-and-forget
        `play` returns as soon as ARI ACCEPTS the playback — so the interpreter advanced to
        `dial`, originated the outbound leg and bridged it while the notice was still
        mid-sentence. Observed live on this flow: flow.node.play and flow.node.dial were
        emitted 13ms apart. FL is all-party consent (ARCHITECTURE.md #17), so a notice the
        caller never hears is worse than no notice at all.

        `play_and_wait` (which correlates the WS PlaybackFinished) is the same blocking op the
        unassigned-DID default handler already uses for exactly this reason — see
        `runtime._handle_unassigned`. It is resolved defensively because AriControl's minimum
        surface is the fire-and-forget `play`; a client that implements only that still works,
        just without the wait. Both paths are best-effort in the client: an unplayable prompt
        or a missed finished-event returns rather than dead-airing the caller."""
        play_and_wait = getattr(self.ari, "play_and_wait", None)
        if play_and_wait is None:
            await self.ari.play(self.channel_id, media)
            return
        await play_and_wait(self.channel_id, media)

    async def _h_hours(self, node: dict) -> Optional[str]:
        cfg = node.get("hours") or node.get("business_hours") or {}
        tz_name = (cfg.get("tz") if isinstance(cfg, dict) else None) or node.get("tz") or self.business_tz
        at = self.now()
        is_open = evaluate_hours(node, at, self.business_tz)
        # An hours node fails OPEN when it has no schedule, so "why did an after-hours caller
        # reach the greeting?" has two very different answers — genuinely open, or no schedule
        # configured at all. Record the timezone and the local time it judged against: an hours
        # node evaluated in the wrong zone is otherwise invisible until someone complains.
        try:
            local = at.astimezone(ZoneInfo(str(tz_name)))
            local_time = local.strftime("%Y-%m-%dT%H:%M")
            dow = _DOW[local.weekday()]
        except Exception:  # noqa: BLE001 - unknown tz: the evaluation already fell back
            local_time, dow = None, None
        schedule = (cfg.get("schedule") or cfg.get("weekly") or {}) if isinstance(cfg, dict) else {}
        self._event_extra = {
            "hours_open": is_open,
            "hours_tz": str(tz_name),
            "hours_local_time": local_time,
            "hours_dow": dow,
            "hours_configured": bool(isinstance(schedule, dict) and schedule),
        }
        return "open" if is_open else "closed"

    async def _h_menu(self, node: dict) -> Optional[str]:
        media = self._interp(self._media(node))
        timeout_s = float(node.get("timeout", 5))
        max_digits = int(node.get("max_digits", 1))
        digit = await self.ari.read_digit(
            self.channel_id, prompt=media, timeout_s=timeout_s, max_digits=max_digits
        )
        # Ticket 17: the collected digits become a flow variable ("" on timeout/no input).
        self.variables["gather.digits"] = digit or ""
        edges = node.get("next") if isinstance(node.get("next"), dict) else {}
        # A menu's outcome is the single most diagnostic fact about an IVR call, and "which
        # port did this take" is NOT recoverable afterwards from the digits alone: `timeout`
        # (heard the prompt, pressed nothing) and `invalid` (pressed an unwired key) are very
        # different problems with very different fixes, and both just end the call. `options`
        # records what the caller COULD have pressed, so the event explains itself without
        # anyone having to fetch and read the pinned version's graph JSON.
        self._event_extra = {
            "digits": digit or None,
            "timeout_s": timeout_s,
            "max_digits": max_digits,
            "options": sorted(k for k in edges if len(str(k)) == 1 and str(k).isdigit()),
        }
        if not digit:
            return "timeout"          # no input; routes via 'timeout' port or falls through
        if digit in edges:
            return digit              # wired DTMF option
        return "invalid" if "invalid" in edges else digit  # unwired digit -> fallback

    async def _h_dial(self, node: dict) -> Optional[str]:
        kind = node.get("target_kind") or node.get("kind")
        timeout_s = float(node.get("timeout", 25))
        caller_id = node.get("caller_id")
        # `record` on a DIAL node records the BRIDGE, after both legs are joined — it does NOT
        # start a channel recording here. Starting one on the caller's channel first is what
        # the old code did, and Asterisk then rejects the bridge with
        #   409 {"message":"Channel <id> currently recording"}
        # so the caller and the dialled party never got connected: 25s of dead air with a
        # caller-only recording (live call 1785953643.61). `ring_and_bridge` always recorded
        # the bridge for this reason; the dial path now does the same.
        record_name = self._rec_name("dial") if node.get("record") else None

        # Operator-target (Ticket 13): dial one operator (individual) or a group of operators
        # (first-to-answer). An offline/unavailable operator never answers, so the unwired/
        # 'noanswer' port falls through to default_fallback — never dead air.
        if kind == "operator":
            operators = _operator_list(node)
            if not operators:
                logger.warning(
                    "interpreter %s: operator dial node has no operators configured", self.linkedid
                )
                self._event_extra = {"dial_kind": "operator", "dial_operators": 0}
                return _ERROR  # operator target with no operators configured
            result = await self.ari.dial_operator(
                self.channel_id, operators, caller_id=caller_id, timeout_s=timeout_s,
                record_name=record_name,
            )
            self._event_extra = {
                "dial_kind": "operator",
                "dial_operators": len(operators),
                "dial_result": _port_label(result),
                "dial_timeout_s": timeout_s,
                "recorded": bool(record_name),
                **self._dial_diagnostics(),
            }
            return result

        # NUMBER target (default). `target`/`number` holds the E.164 to reach over the trunk;
        # {{var}} templates (e.g. a number captured into a variable) interpolate first.
        target = self._interp(node.get("target") or node.get("number")).strip()
        if not target:
            # An empty target is almost always an unresolved {{var}}, not an empty config
            # field — worth naming loudly, because the caller silently falls through.
            logger.warning(
                "interpreter %s: dial node resolved an EMPTY target from %r",
                self.linkedid, node.get("target") or node.get("number"),
            )
            self._event_extra = {"dial_kind": "number", "dial_target": None}
            return _ERROR
        result = await self.ari.dial_number(
            self.channel_id, target, caller_id=caller_id, timeout_s=timeout_s,
            record_name=record_name,
        )
        # `dial_result` is the outcome the caller actually experienced (rang out, busy, trunk
        # failure) — paired with the exit event's `ms` it is also the ring duration, which is
        # what tells "nobody picked up" apart from "the trunk rejected the call".
        self._event_extra = {
            "dial_kind": "number",
            "dial_target": _snap(target),
            "dial_result": _port_label(result),
            "dial_timeout_s": timeout_s,
            "recorded": bool(record_name),
            **self._dial_diagnostics(),
        }
        self._warn_if_ports_unreachable(node, target)
        return result  # "answered" | "noanswer" | "busy" | "failed"

    def _warn_if_ports_unreachable(self, node: dict, target: str) -> None:
        """Warn when this dial's call-coverage ports CANNOT fire for this destination.

        A CPaaS-hosted number (Twilio/Bandwidth, and so Quo/OpenPhone, Google Voice, a hosted
        PBX) returns 200 OK within a second to run its own app logic, then rings the human
        behind in-band ringback. Asterisk sees an ANSWERED call immediately, so the node's ring
        timeout never expires and its `noanswer`/`busy` ports are dead wire — while the
        operator believes they are the path to voicemail or the next number in the list.

        Nothing distinguishes that from a real pickup at the SIP layer, so this cannot be fixed
        — only surfaced. WARNING level so it lands in /api/ai/errors, and only when a coverage
        port is actually wired, so a plain redirect flow stays quiet."""
        extra = self._event_extra or {}
        if not extra.get("dial_answer_platform"):
            return
        edges = node.get("next") if isinstance(node.get("next"), dict) else {}
        unreachable = [p for p in ("noanswer", "busy") if edges.get(p)]
        if not unreachable:
            return
        logger.warning(
            "interpreter %s: dial to %s was answered by the DESTINATION PLATFORM in %sms, not "
            "by a person — its %s port(s) can never fire for this target, so that call "
            "coverage will never run. Route coverage on the far end instead.",
            self.linkedid, target, extra.get("dial_answer_ms"), "/".join(unreachable),
        )
        self._event_extra["dial_ports_unreachable"] = unreachable

    def _dial_diagnostics(self) -> dict:
        """The client's post-mortem of the dial that just finished — who hung up, with which
        Q.850, how fast the far end answered — merged onto the dial node's exit event.

        Resolved defensively (like `play_and_wait`) because `pop_dial_diagnostics` is NOT part
        of the minimum `AriControl` surface: a client or test fake that does not implement it
        still dials, it just exits without the forensics. Never raises — a diagnostic must not
        be able to break a call it is only describing."""
        pop = getattr(self.ari, "pop_dial_diagnostics", None)
        if pop is None:
            return {}
        try:
            data = pop()
        except Exception:  # noqa: BLE001 - diagnostics are never worth failing a node over
            logger.exception("interpreter %s: dial diagnostics unavailable", self.linkedid)
            return {}
        return data if isinstance(data, dict) else {}

    async def _h_voicemail(self, node: dict) -> Optional[str]:
        # Real voicemail capture (Ticket 18): greeting -> beep -> record until the caller hangs
        # up or falls silent (capped), then hang up. Delegated to the ARI client's `voicemail`
        # (which blocks for the message); the old stub started a recording then immediately hung
        # up, capturing nothing. Terminal node — the caller leaves a message, the flow ends.
        media = self._interp(self._media(node) or self._media_key(node, "greeting")) or None
        max_duration = float(node.get("max_duration", _VM_MAX_DURATION_S))
        max_silence = float(node.get("max_silence", _VM_MAX_SILENCE_S))
        name = self._rec_name("vm")
        await self.ari.voicemail(
            self.channel_id,
            greeting=media,
            name=name,
            max_duration_s=max_duration,
            max_silence_s=max_silence,
        )
        # `recording_name` is the join key back to the `recordings` row, so a voicemail that
        # left no audio is traceable to the call that produced it. The exit event's `ms` is
        # the tell: a caller who hangs up as the greeting starts yields a few hundred ms and
        # no message — indistinguishable, without this, from voicemail never running.
        self._event_extra = {
            "recording_name": name,
            "greeting": bool(media),
            "max_duration_s": max_duration,
            "max_silence_s": max_silence,
        }
        return None  # terminal

    async def _h_hangup(self, node: dict) -> Optional[str]:
        await self.ari.hangup(self.channel_id)
        return None  # terminal

    async def _h_ai_agent(self, node: dict) -> Optional[str]:
        # Run a VoiceAgentSession via the injected `run_agent` seam (Ticket 11): it resolves +
        # PINS the node's agent_version, runs the session (dummy engine for now), and returns
        # the exit PORT + any tool data. The agent NEVER bridges — we just route by the port,
        # which _resolve wires to the node's `next` (unwired/`failed` falls through to
        # default_fallback). Any failure -> `failed`. The engine vocabulary says "end_call"
        # (the tool name) but the GRAPH port is "complete" (Ticket 15.4) — mapped here, at
        # the engine↔graph seam, so validator and engine stay aligned on
        # {default, transfer, complete, failed}. When no seam is injected the node keeps its
        # legacy stub (route to `default`).
        if self.run_agent is None:
            return "default"
        try:
            port, _data = await self.run_agent(node)
        except Exception:  # noqa: BLE001 - an agent failure must take the `failed` port, not dead-air
            logger.exception("interpreter %s: ai_agent session failed", self.linkedid)
            return "failed"
        if port == "end_call":
            return "complete"
        # `taken_over` passes through untranslated: _run_graph recognises it and stands down.
        return port or "failed"

    # --- Ticket 17 parity nodes ---

    async def _h_set_vars(self, node: dict) -> Optional[str]:
        # config: {"vars": {name: "literal or {{var}}"}}. String values interpolate against
        # the current store (so `greeting = "Hi {{caller_number}}"` works); non-strings are
        # stored as-is. Insertion order of the config dict is the assignment order.
        cfg = node.get("vars")
        snapshot: dict = {}
        if isinstance(cfg, dict):
            for name, value in cfg.items():
                if not name:
                    continue
                key = str(name)
                self.variables[key] = interpolate(value, self.variables) if isinstance(value, str) else value
                snapshot[key] = _snap(self.variables[key])
        self._event_extra = {"vars_set": snapshot}
        return "default"

    async def _h_unset_vars(self, node: dict) -> Optional[str]:
        # config: {"names": ["a", "b"]}. Unknown names are a no-op.
        names = node.get("names")
        removed: list[str] = []
        if isinstance(names, (list, tuple)):
            for n in names:
                key = str(n) if n else ""
                if key and key in self.variables:
                    self.variables.pop(key)
                    removed.append(key)
        self._event_extra = {"vars_unset": removed}
        return "default"

    async def _h_conditions(self, node: dict) -> Optional[str]:
        # Ordered rows, first match wins; no match -> "else". Evaluation is pure (see
        # app/flows/variables.py) and never raises — bad regexes/malformed rows are skipped.
        rows = node.get("rows") if isinstance(node.get("rows"), list) else []
        idx, port, actual = evaluate_conditions(rows, self.variables)
        if port is None:
            self._event_extra = {"matched_row": None, "port": "else"}
            return "else"
        row = rows[idx] if isinstance(rows[idx], dict) else {}
        self._event_extra = {
            "matched_row": idx,
            "port": port,
            "variable": _snap(row.get("variable")),
            "operator": _snap(row.get("operator")),
            "actual": _snap(actual),
        }
        return port

    async def _h_send_sms(self, node: dict) -> Optional[str]:
        # Fire-and-forget: the injected seam SCHEDULES the send through the platform outbound
        # SMS service (from = the flow's DID; opt-out + 10DLC gates apply there) and returns
        # immediately. The `default` port is taken regardless of send outcome — an SMS
        # problem must never stall or reroute the call.
        to = interpolate(node.get("to") or "{{caller_number}}", self.variables).strip()
        body = interpolate(node.get("body"), self.variables).strip()
        self._event_extra = {"sms_to": _snap(to), "sms_body": _snap(body)}
        if not to or not body:
            logger.warning("interpreter %s: send_sms node missing to/body; skipping", self.linkedid)
            return "default"
        if self.send_sms is None:
            logger.warning("interpreter %s: send_sms node has no sender seam; skipping", self.linkedid)
            return "default"
        try:
            await self.send_sms(to, body)
        except Exception:  # noqa: BLE001 - fire-and-forget: an SMS failure never reroutes the call
            logger.exception("interpreter %s: send_sms scheduling failed", self.linkedid)
        return "default"

    async def _h_request(self, node: dict) -> Optional[str]:
        # HTTP GET/POST via the injected seam (runtime: httpx, 5s hard timeout). 2xx ->
        # "success" and request.status / request.body populate the store (dot-path readable,
        # e.g. {{request.body.data.status}}); anything else -> "failure" with request.status
        # set (0 for transport errors/timeouts/missing config).
        method = str(node.get("method") or "GET").upper()
        if method not in ("GET", "POST"):
            method = "GET"
        url = interpolate(node.get("url"), self.variables).strip()
        raw_headers = node.get("headers") if isinstance(node.get("headers"), dict) else {}
        headers = {
            interpolate(k, self.variables): interpolate(v, self.variables)
            for k, v in raw_headers.items()
            if k
        }
        body = node.get("body")
        if isinstance(body, str):
            body = interpolate(body, self.variables)

        status, parsed = 0, None
        if url and self.http_request is not None:
            try:
                status, parsed = await self.http_request(method, url, headers, body)
                status = int(status or 0)
            except Exception:  # noqa: BLE001 - a transport error takes the failure port
                logger.exception("interpreter %s: request node failed (%s %s)", self.linkedid, method, url)
                status, parsed = 0, None
        elif not url:
            logger.warning("interpreter %s: request node has no url", self.linkedid)
        else:
            logger.warning("interpreter %s: request node has no http seam", self.linkedid)

        self.variables["request.status"] = status
        self.variables["request.body"] = parsed
        self._event_extra = {"request_status": status, "request_url": _snap(url)}
        return "success" if 200 <= status < 300 else "failure"

    _HANDLERS: ClassVar[dict[str, Callable[["FlowInterpreter", dict], Awaitable[Optional[str]]]]] = {
        "entry": _h_entry,
        "play": _h_play,
        "hours": _h_hours,
        "menu": _h_menu,
        "dial": _h_dial,
        "voicemail": _h_voicemail,
        "hangup": _h_hangup,
        "ai_agent": _h_ai_agent,
        "set_vars": _h_set_vars,
        "unset_vars": _h_unset_vars,
        "conditions": _h_conditions,
        "send_sms": _h_send_sms,
        "request": _h_request,
    }

    # --- misc ---

    @staticmethod
    def _media_key(node: dict, key: str) -> Optional[str]:
        v = node.get(key)
        return str(v) if v else None

    def _media(self, node: dict) -> Optional[str]:
        return self._media_key(node, "media") or self._media_key(node, "prompt")

    def _interp(self, text: Optional[str]) -> str:
        """Interpolate {{var}} templates against this call's variable store ("" for None).

        Interpolated prompt text deliberately BYPASSES the activation-time TTS prewarm
        (which skips {{...}}): the downstream play path synthesizes lazily and caches by
        the INTERPOLATED text, so repeated values still hit the TTS cache."""
        return interpolate(text, self.variables)

    def _rec_name(self, tag: str) -> str:
        self._rec_counter += 1
        return f"{self.linkedid}-{tag}-{self._rec_counter}"

    async def _emit_transition(
        self, step: int, node_id: str, ntype: Optional[str], extra: Optional[dict] = None
    ) -> None:
        """Emit EXACTLY ONE call_event for entering this node. The dedup key
        `{linkedid}:{step}:{node_id}` is unique per transition (a node revisited in a loop
        gets a fresh step), matching call_events' (call_id, event_type, provider_sequence).
        `extra` (Ticket 17 deferred-emit nodes) merges an outcome snapshot into the payload
        — variable values are pre-truncated to _SNAP_MAX chars by the handlers."""
        seq = f"{self.linkedid}:{step}:{node_id}"
        flow: dict = {
            "step": step,
            "node_id": node_id,
            "node_type": ntype,
            "linkedid": self.linkedid,
        }
        if extra:
            flow.update(extra)
        await self.emit(f"flow.node.{ntype}", seq, {"flow": flow})

    async def _emit_exit(
        self, step: int, node_id: str, ntype: Optional[str], port: Optional[str],
        routed: str, next_id: Optional[str], ms: int, errored: bool,
    ) -> None:
        """Emit ONE `flow.node.exit` per node, recording WHY the call left it.

        The entry event (`flow.node.<type>`) is written BEFORE the handler runs, so it can
        never carry an outcome — which is exactly the gap that made "did this caller time out
        or press an unwired digit?" unanswerable from the event log alone. This is the other
        half: the port the handler chose, where that port routed, how long the node took, and
        the handler's own detail snapshot (`_event_extra`: menu digits, dial result, …).

        `routed` is the ROUTING DECISION, which is not derivable from the port alone without
        also reading the version's graph:
          edge      -> the port was wired; `next` is its target
          fallback  -> unwired/errored port; `next` is the flow's default_fallback
          hangup    -> unwired/errored port and NO fallback: the caller was hung up on
          terminal  -> a voicemail/hangup node ended the call

        Best-effort by construction: instrumentation must never be able to dead-air a caller,
        so a failed write is logged and swallowed rather than aborting the flow.
        """
        try:
            flow: dict = {
                "step": step,
                "node_id": node_id,
                "node_type": ntype,
                "linkedid": self.linkedid,
                "port": _port_label(port),
                "routed": routed,
                "next": next_id,
                "ms": ms,
            }
            if errored:
                flow["errored"] = True
            if self._event_extra:
                flow.update(self._event_extra)
            await self.emit("flow.node.exit", f"{self.linkedid}:{step}:{node_id}:exit", {"flow": flow})
        except Exception:  # noqa: BLE001 - observability must never break a live call
            logger.exception("interpreter %s: exit event for '%s' failed", self.linkedid, node_id)

    async def _emit_summary(self, reason: str, ms: int) -> None:
        """Emit ONE `flow.call.summary` per call: the whole path and how it ended.

        One row per call, so "why did calls end this way today" is a plain GROUP BY instead of
        a window function over per-node events. `ended` is the reason from `_run_graph`:
        terminal (a voicemail/hangup node), dial_completed (a dial ANSWERED and the flow had
        nothing wired after it — the normal end of a redirect flow, caller was served),
        unrouted_hangup (an unwired port with no fallback and the caller was NOT connected to
        anything — a dropped caller), max_steps, dangling_edge, empty_graph, no_entry,
        completed, or error (the interpreter itself raised).

        dial_completed and unrouted_hangup were one value until a live redirect flow made the
        distinction obvious: every successful forward was being counted as a dropped call.

        Best-effort for the same reason as `_emit_exit`, and it runs in `run`'s `finally`, so
        a call that dies mid-node still leaves a summary describing how far it got.
        """
        try:
            path = list(self._path)
            flow: dict = {
                "linkedid": self.linkedid,
                "ended": reason,
                "steps": self._steps,
                "path": path,
                "terminal_node": self._last_node,
                "ms": ms,
            }
            if self._steps > len(path):
                flow["path_truncated"] = True
            await self.emit("flow.call.summary", f"{self.linkedid}:summary", {"flow": flow})
        except Exception:  # noqa: BLE001 - observability must never break a live call
            logger.exception("interpreter %s: summary event failed", self.linkedid)

    async def _safe_hangup(self) -> None:
        try:
            await self.ari.hangup(self.channel_id)
        except Exception:  # noqa: BLE001 - hangup is best-effort at end-of-flow
            logger.exception("interpreter %s: final hangup failed", self.linkedid)

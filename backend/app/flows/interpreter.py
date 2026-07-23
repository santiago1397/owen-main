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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, ClassVar, Optional, Protocol
from zoneinfo import ZoneInfo

from app.flows.variables import evaluate_conditions, interpolate

logger = logging.getLogger("flows.interpreter")

# Node types that TERMINATE the call: once run, the interpreter stops (no onward routing).
TERMINAL_TYPES: frozenset[str] = frozenset({"voicemail", "hangup"})

# Ticket 17 parity nodes emit their transition event AFTER the handler runs (instead of the
# usual emit-on-entry), so the payload can snapshot the OUTCOME (vars set, matched condition
# row, request status). All of these run in bounded time (send_sms is fire-and-forget; the
# request node has a hard timeout), so deferring the emit never delays it meaningfully.
_POST_EMIT_TYPES: frozenset[str] = frozenset(
    {"set_vars", "unset_vars", "conditions", "send_sms", "request"}
)

# Event-payload snapshot cap: variable VALUES in flow.node.* payloads are truncated to this.
_SNAP_MAX = 200

# Sentinel port meaning "the handler could not choose a valid port" (unknown node type or a
# handler error). It never matches a wired edge, so it always falls through to the fallback.
_ERROR: str = "\x00__error__"

# Weekday index (Mon=0) -> the schedule key an `hours` node uses.
_DOW = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _snap(value: Any) -> str:
    """A value as it appears in a flow.node.* event payload: str()'d, capped at _SNAP_MAX."""
    return "" if value is None else str(value)[:_SNAP_MAX]


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
    async def record(self, channel_id: str, name: str) -> None: ...
    async def read_digit(
        self, channel_id: str, *, prompt: Optional[str], timeout_s: float, max_digits: int
    ) -> Optional[str]: ...
    async def dial_number(
        self, channel_id: str, number: str, *, caller_id: Optional[str], timeout_s: float
    ) -> str: ...
    async def dial_operator(
        self, channel_id: str, operators: list, *, caller_id: Optional[str], timeout_s: float
    ) -> str: ...
    async def hangup(self, channel_id: str) -> None: ...


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
    # Outcome snapshot the deferred-emit node handlers stash for their transition event.
    _event_extra: Optional[dict] = field(default=None, init=False)

    async def run(self) -> None:
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
            await self._safe_hangup()
            return

        fallback = self.graph.get("default_fallback")
        fallback = fallback if isinstance(fallback, str) and fallback in nodes else None

        current: Optional[str] = self._entry_id(nodes)
        step = 0
        while current is not None:
            if step >= self.max_steps:
                logger.warning("interpreter %s exceeded max_steps; hanging up", self.linkedid)
                await self._safe_hangup()
                return
            step += 1

            node = nodes.get(current)
            if not isinstance(node, dict):
                # Dangling target: fall to fallback once, else hang up.
                current, fallback = self._fall(fallback)
                if current is None:
                    await self._safe_hangup()
                    return
                continue

            ntype = node.get("type")
            # Ticket 17 parity nodes emit AFTER the handler so the event snapshots the
            # outcome; everything else keeps the original emit-on-entry.
            post_emit = ntype in _POST_EMIT_TYPES
            if not post_emit:
                await self._emit_transition(step, current, ntype)

            self._event_extra = None
            try:
                port = await self._run_node(node, ntype)
            except Exception:  # noqa: BLE001 - a node failure must fall through, not dead-air
                logger.exception("interpreter %s: node '%s' (%s) failed", self.linkedid, current, ntype)
                port = _ERROR

            if post_emit:
                await self._emit_transition(step, current, ntype, extra=self._event_extra)

            if ntype in TERMINAL_TYPES:
                return  # voicemail / hangup already terminated the channel

            nxt = self._resolve(node, port)
            if nxt is not None:
                current = nxt
            else:
                # Unwired or errored port -> the flow-level fallback (once), else clean hangup.
                current, fallback = self._fall(fallback)
                if current is None:
                    await self._safe_hangup()
                    return

        await self._safe_hangup()

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
            await self.ari.play(self.channel_id, media)
        return "default"

    async def _h_hours(self, node: dict) -> Optional[str]:
        return "open" if evaluate_hours(node, self.now(), self.business_tz) else "closed"

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
        if not digit:
            return "timeout"          # no input; routes via 'timeout' port or falls through
        if digit in edges:
            return digit              # wired DTMF option
        return "invalid" if "invalid" in edges else digit  # unwired digit -> fallback

    async def _h_dial(self, node: dict) -> Optional[str]:
        kind = node.get("target_kind") or node.get("kind")
        timeout_s = float(node.get("timeout", 25))
        caller_id = node.get("caller_id")
        if node.get("record"):
            await self.ari.record(self.channel_id, self._rec_name("dial"))

        # Operator-target (Ticket 13): dial one operator (individual) or a group of operators
        # (first-to-answer). An offline/unavailable operator never answers, so the unwired/
        # 'noanswer' port falls through to default_fallback — never dead air.
        if kind == "operator":
            operators = _operator_list(node)
            if not operators:
                return _ERROR  # operator target with no operators configured
            return await self.ari.dial_operator(
                self.channel_id, operators, caller_id=caller_id, timeout_s=timeout_s
            )

        # NUMBER target (default). `target`/`number` holds the E.164 to reach over the trunk;
        # {{var}} templates (e.g. a number captured into a variable) interpolate first.
        target = self._interp(node.get("target") or node.get("number")).strip()
        if not target:
            return _ERROR
        result = await self.ari.dial_number(
            self.channel_id, target, caller_id=caller_id, timeout_s=timeout_s
        )
        return result  # "answered" | "noanswer" | "busy" | "failed"

    async def _h_voicemail(self, node: dict) -> Optional[str]:
        media = self._interp(self._media(node) or self._media_key(node, "greeting")) or None
        if media:
            await self.ari.play(self.channel_id, media)
        await self.ari.record(self.channel_id, self._rec_name("vm"))
        await self.ari.hangup(self.channel_id)
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

    async def _safe_hangup(self) -> None:
        try:
            await self.ari.hangup(self.channel_id)
        except Exception:  # noqa: BLE001 - hangup is best-effort at end-of-flow
            logger.exception("interpreter %s: final hangup failed", self.linkedid)

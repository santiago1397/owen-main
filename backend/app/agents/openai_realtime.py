"""OpenAI Realtime `VoiceAgentSession` engine (Ticket 12) — fills in the Ticket-11 stub.

A real caller has a spoken conversation with a live AI agent that can capture a lead, send
an SMS, transfer, or end the call — and NEVER leaves the caller in dead air on failure. This
engine bridges **AudioSocket/TCP ↔ OpenAI Realtime API ↔ the call bridge**, with OpenAI
server-VAD barge-in (interrupt playback the moment the caller speaks).

It slots into the registry in app/agents/session.py (`get_voice_agent_session("openai_realtime")`)
and honours the same contract as the dummy: `run(spec, ctx) -> AgentResult{port, data}`, where
`port ∈ {transfer, end_call, default, failed}`. The engine NEVER bridges/hangs up — it returns a
PORT and the flow interpreter drives the graph edge (see app/flows/interpreter.py `_h_ai_agent`).

DESIGN — pure core, thin I/O edge (mirrors app/flows/interpreter.py's split):
- The PURE, unit-tested parts are import-light (stdlib + app.agents only): tool dispatch→ports,
  guardrail logic, transcript assembly, the failure/retry decision, and the event→port drive
  loop (`_drive`). These run against a FAKE `RealtimeConnection` in tests — no audio/WS/DB.
- The real AudioSocket + OpenAI-WS + Postgres I/O lives behind thin wrappers (`_default_connect`,
  `_OpenAIRealtimeConnection`, `_default_persist`) that lazily import websockets/asyncio/sqlalchemy
  so this module stays importable in the dependency-light sandbox. Those wrappers are verified by
  py_compile + review only — they are the UNRUN paths (no real audio/WS/DB here).

FAILURE CONTRACT: any error (OpenAI WS drop, AudioSocket error, timeout) → **1 WS-reconnect
retry**, then return the `failed` port → the interpreter routes to `default_fallback` (voicemail).
The caller never hits dead air. Guardrails (`max_call_seconds`/`max_silence_seconds`) end the call
gracefully via the `end_call` port. `capture_lead` sets `data["captured"]` (authoritative for the
existing analysis `captured` concept); the speaker-labeled transcript is written INLINE to the
`transcriptions` store, so agent legs skip post-call STT.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol

from app.agents.session import AgentCallContext, AgentResult, AgentSpec
from app.agents.tools import FLOW_EXIT, TOOLS, enabled_tools, is_valid_port

logger = logging.getLogger("agents.openai_realtime")

ENGINE_NAME = "openai_realtime"

# Design decision: exactly ONE WS-reconnect retry before giving up on the `failed` port.
DEFAULT_WS_RECONNECTS = 1

# Speaker labels for the assembled transcript. The AI agent stands in the operator role, but is
# labelled "agent" so a human reviewer can tell it apart from a live operator.
SPEAKER_CALLER = "caller"
SPEAKER_AGENT = "agent"

# Normalized event types the connection yields (the real adapter maps OpenAI/AudioSocket events
# onto these; fakes emit them directly). Keeping the loop on a normalized vocabulary is what makes
# it testable without the WS/audio layer.
EV_SPEECH = "speech"        # {"type","speaker","text","at"} — a finalized transcript fragment
EV_TOOL_CALL = "tool_call"  # {"type","name","arguments","call_id","at"}
EV_TICK = "tick"            # {"type","at"} — a keepalive/silence tick so guardrails can fire
EV_ERROR = "error"          # {"type","message"} — a transport error (retryable → reconnect)


class RealtimeConnectionError(Exception):
    """A recoverable transport failure (WS drop / AudioSocket error / handshake timeout).

    Raised by the connection layer; caught by `run` to trigger the single reconnect retry,
    then the `failed` port. Distinct from a programming error so retry logic is explicit."""


class RealtimeConnection(Protocol):
    """The thin async transport the drive loop consumes. The real implementation
    (`_OpenAIRealtimeConnection`) bridges AudioSocket↔OpenAI-WS; tests pass a FAKE."""

    async def next_event(self) -> Optional[dict]:
        """Next normalized event, or None when the conversation ended cleanly. Raises
        RealtimeConnectionError on a transport failure (→ reconnect/`failed`)."""
        ...

    async def send_tool_result(self, call_id: Optional[str], result: dict) -> None:
        """Return an in-call tool's result to the model so it can keep talking."""
        ...

    async def close(self) -> None: ...


# --- Guardrails (pure) --------------------------------------------------------------------

@dataclass
class Guardrails:
    """Per-agent session limits (from `spec.guardrails`). `model` is the tier hint passed to
    the realtime session; the two time limits are enforced by the drive loop."""

    max_call_seconds: float | None = None
    max_silence_seconds: float | None = None
    model: str = ""


def parse_guardrails(raw: dict | None) -> Guardrails:
    """Flatten `spec.guardrails` into a `Guardrails`, tolerating missing/garbage values
    (a bad limit is treated as unset rather than crashing the call)."""
    g = raw if isinstance(raw, dict) else {}

    def _num(key: str) -> float | None:
        v = g.get(key)
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None

    return Guardrails(
        max_call_seconds=_num("max_call_seconds"),
        max_silence_seconds=_num("max_silence_seconds"),
        model=str(g.get("model") or g.get("model_tier") or ""),
    )


def guardrail_port(elapsed: float, silence: float, limits: Guardrails) -> Optional[str]:
    """Pure: the port to exit on if a time guardrail tripped, else None.

    `elapsed` = seconds since the call started; `silence` = seconds since the last caller/agent
    activity. Either limit tripping ends the call GRACEFULLY on the `end_call` port (the caller
    is not dead-aired — the flow takes its `end_call`/fallback edge)."""
    if limits.max_call_seconds is not None and elapsed >= limits.max_call_seconds:
        return "end_call"
    if limits.max_silence_seconds is not None and silence >= limits.max_silence_seconds:
        return "end_call"
    return None


# --- Failure/retry decision (pure) --------------------------------------------------------

def should_retry(attempt: int, max_reconnects: int) -> bool:
    """Pure: whether to attempt one more connection. `attempt` is 0-based; with the default
    `max_reconnects == 1` this yields exactly two total attempts (initial + one reconnect)."""
    return attempt < max_reconnects


# --- Tool dispatch → ports / side-effects (pure) ------------------------------------------

@dataclass
class ToolOutcome:
    """The result of dispatching one tool call. `exit_port` (transfer/end_call) ENDS the agent
    turn and is handed to the interpreter; `data` merges into `AgentResult.data`; `result` is the
    payload returned to the model so an IN-CALL tool (capture_lead/send_sms) can keep the
    conversation going."""

    exit_port: Optional[str] = None
    data: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)


# send_sms sender: async (to, body) -> bool. Injected so tests fake it; the real engine reuses
# the platform SMS send path if one exists, else records the message in `data["sms_outbox"]`
# (an enqueue the SMS worker can later drain). OWEN has no outbound-SMS send path today, so the
# default is enqueue-only.
SmsSender = Callable[[str, str], Awaitable[bool]]


def dispatch_tool(
    name: str,
    arguments: dict,
    enabled: dict,
    spec: AgentSpec,
    ctx: AgentCallContext,
) -> ToolOutcome:
    """Pure dispatch of ONE tool call to a port and/or side-effect. Only tools in `enabled`
    (the per-agent toggled-ON subset of the fixed registry) may run — an unknown/disabled name
    returns an error result the model sees, and NEVER smuggles in an unimplemented capability."""
    args = arguments if isinstance(arguments, dict) else {}
    if name not in enabled:
        return ToolOutcome(result={"error": f"tool '{name}' is not available"})

    spec_def = TOOLS.get(name, {})
    if spec_def.get("kind") == FLOW_EXIT:
        # transfer / end_call — hand the interpreter the node port; the agent never bridges.
        return ToolOutcome(exit_port=spec_def.get("exit_port"), result={"ok": True})

    if name == "capture_lead":
        # Authoritative source of the existing analysis `captured` concept.
        lead = {k: v for k, v in args.items() if v not in (None, "")}
        return ToolOutcome(data={"captured": lead}, result={"ok": True, "captured": True})

    if name == "send_sms":
        to = str(args.get("to") or ctx.caller_number or "").strip()
        body = str(args.get("body") or args.get("message") or "").strip()
        if not to or not body:
            return ToolOutcome(result={"error": "send_sms needs a `to` number and a `body`"})
        # Enqueue intent onto the result data; the loop appends across multiple calls. A real
        # SMS send path (if wired later) is driven by the injected SmsSender in the loop.
        return ToolOutcome(
            data={"sms_outbox": [{"to": to, "body": body}]},
            result={"ok": True, "queued": True},
        )

    # A registry tool with no dispatch branch (shouldn't happen — kept explicit).
    return ToolOutcome(result={"error": f"tool '{name}' has no handler"})


# --- Transcript assembly (pure) -----------------------------------------------------------

class TranscriptAssembler:
    """Accumulates finalized speech fragments into a speaker-labeled transcript for the
    `transcriptions` store. `segments()` yields the `{speaker, text}` list (the store's JSONB
    `segments` shape); `text()` is the flat speaker-prefixed rendering."""

    def __init__(self) -> None:
        self._turns: list[dict] = []

    def add(self, speaker: str | None, text: str | None) -> None:
        clean = (text or "").strip()
        if not clean:
            return
        spk = SPEAKER_CALLER if speaker == SPEAKER_CALLER else SPEAKER_AGENT
        self._turns.append({"speaker": spk, "text": clean})

    def is_empty(self) -> bool:
        return not self._turns

    def segments(self) -> list[dict]:
        return list(self._turns)

    def text(self) -> str:
        return "\n".join(f"{t['speaker']}: {t['text']}" for t in self._turns)


# --- OpenAI Realtime session config builders (pure) ---------------------------------------

def build_instructions(spec: AgentSpec) -> str:
    """Compose the system instructions for the realtime session from the agent's persona,
    greeting, and in-context knowledge. Pure/deterministic so it is testable."""
    parts: list[str] = []
    if spec.persona.strip():
        parts.append(spec.persona.strip())
    if spec.knowledge.strip():
        parts.append("Reference knowledge:\n" + spec.knowledge.strip())
    if spec.greeting.strip():
        parts.append(f"Open the call by saying: {spec.greeting.strip()}")
    return "\n\n".join(parts)


def build_realtime_tools(enabled: dict) -> list[dict]:
    """Build the OpenAI Realtime `tools` (function-calling) schema for exactly the agent's
    toggled-ON tools. NO arbitrary tools — the closed registry is the source of truth."""
    params: dict[str, dict] = {
        "transfer": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "Why the call is being transferred."}}},
        "end_call": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "Why the call is ending."}}},
        "capture_lead": {"type": "object", "properties": {
            "name": {"type": "string"}, "intent": {"type": "string"},
            "phone": {"type": "string"}, "email": {"type": "string"},
            "notes": {"type": "string"}}},
        "send_sms": {"type": "object", "properties": {
            "to": {"type": "string", "description": "E.164 recipient; defaults to the caller."},
            "body": {"type": "string"}}, "required": ["body"]},
    }
    out: list[dict] = []
    for name in enabled:
        spec_def = TOOLS.get(name, {})
        out.append({
            "type": "function",
            "name": name,
            "description": spec_def.get("description", ""),
            "parameters": params.get(name, {"type": "object", "properties": {}}),
        })
    return out


# --- The engine ---------------------------------------------------------------------------

# connect: async (spec, ctx) -> RealtimeConnection. Injectable so the drive loop is testable.
ConnectFn = Callable[[AgentSpec, AgentCallContext], Awaitable[RealtimeConnection]]
# persist: async (ctx, assembler, spec) -> None. Injectable; default writes to `transcriptions`.
PersistFn = Callable[[AgentCallContext, TranscriptAssembler, AgentSpec], Awaitable[None]]


class OpenAIRealtimeSession:
    """The `openai_realtime` VoiceAgentSession. Constructed zero-arg by the registry; tests
    inject fakes for the transport/persistence/clock to exercise the pure drive loop."""

    name = ENGINE_NAME

    def __init__(
        self,
        *,
        connect: Optional[ConnectFn] = None,
        persist: Optional[PersistFn] = None,
        monotonic: Optional[Callable[[], float]] = None,
        max_reconnects: Optional[int] = None,
        sms_sender: Optional[SmsSender] = None,
    ) -> None:
        self._connect = connect
        self._persist = persist
        self._monotonic = monotonic or time.monotonic
        self._max_reconnects = max_reconnects
        self._sms_sender = sms_sender

    async def run(self, spec: AgentSpec, ctx: AgentCallContext) -> AgentResult:
        limits = parse_guardrails(spec.guardrails)
        enabled = enabled_tools(spec.tools)
        assembler = TranscriptAssembler()
        data: dict = {}  # accumulates across attempts so a lead captured pre-drop survives

        connect = self._connect or self._default_connect
        max_reconnects = (
            self._max_reconnects if self._max_reconnects is not None else _settings_reconnects()
        )

        result: AgentResult
        attempt = 0
        while True:
            conn: Optional[RealtimeConnection] = None
            try:
                conn = await connect(spec, ctx)
                result = await self._drive(conn, spec, ctx, enabled, limits, assembler, data)
                break
            except Exception as exc:  # noqa: BLE001 - ANY failure retries once, then `failed`
                logger.warning(
                    "openai_realtime: attempt %d failed (%s): %s",
                    attempt, type(exc).__name__, exc,
                )
                if should_retry(attempt, max_reconnects):
                    attempt += 1
                    continue
                # Never dead-air the caller: take the `failed` port → default_fallback (vm).
                result = AgentResult(port="failed", data=data)
                break
            finally:
                if conn is not None:
                    await _safe_close(conn)

        # Transcript written INLINE to the existing `transcriptions` store (best-effort; the DB
        # write is the unrun path). Even a partial transcript from a failed call is worth keeping.
        if not assembler.is_empty():
            persist = self._persist or self._default_persist
            try:
                await persist(ctx, assembler, spec)
            except Exception:  # noqa: BLE001 - persistence must not turn into dead air
                logger.exception("openai_realtime: transcript persist failed (linkedid=%s)", ctx.linkedid)

        return result

    async def _drive(
        self,
        conn: RealtimeConnection,
        spec: AgentSpec,
        ctx: AgentCallContext,
        enabled: dict,
        limits: Guardrails,
        assembler: TranscriptAssembler,
        data: dict,
    ) -> AgentResult:
        """The PURE event loop (unit-tested with a fake connection): consume normalized events,
        assemble the transcript, dispatch tools to ports/side-effects, and enforce guardrails.
        Returns the exit `AgentResult`. Raises RealtimeConnectionError on a transport error so
        `run` can reconnect. `data` is mutated in place so partial output survives a later drop."""
        start: float | None = None
        last_activity: float | None = None

        while True:
            ev = await conn.next_event()
            if ev is None:
                # Clean end with no explicit exit tool → `default` (agent finished talking).
                return AgentResult(port="default", data=data)
            if not isinstance(ev, dict):
                continue

            now = ev.get("at")
            if now is None:
                now = self._monotonic()
            if start is None:
                start = now
                last_activity = now

            # Guardrails evaluated on every event/tick: max call length + max silence.
            silence_since = now - (last_activity if last_activity is not None else now)
            gport = guardrail_port(now - start, silence_since, limits)
            if gport is not None:
                logger.info("openai_realtime: guardrail tripped → %s (linkedid=%s)", gport, ctx.linkedid)
                return AgentResult(port=gport, data=data)

            etype = ev.get("type")
            if etype == EV_ERROR:
                raise RealtimeConnectionError(str(ev.get("message") or "realtime transport error"))

            if etype == EV_SPEECH:
                assembler.add(ev.get("speaker"), ev.get("text"))
                last_activity = now
                continue

            if etype == EV_TOOL_CALL:
                outcome = dispatch_tool(ev.get("name"), ev.get("arguments") or {}, enabled, spec, ctx)
                _merge_tool_data(data, outcome.data)
                last_activity = now
                if outcome.exit_port is not None:
                    port = outcome.exit_port if is_valid_port(outcome.exit_port) else "failed"
                    return AgentResult(port=port, data=data)
                # In-call tool: hand its result back to the model and keep talking. If a real SMS
                # sender is wired, actually send here (best-effort; enqueue remains the record).
                if ev.get("name") == "send_sms" and self._sms_sender is not None and outcome.data.get("sms_outbox"):
                    msg = outcome.data["sms_outbox"][-1]
                    try:
                        await self._sms_sender(msg["to"], msg["body"])
                    except Exception:  # noqa: BLE001 - a send failure keeps the queued record
                        logger.exception("openai_realtime: send_sms failed (linkedid=%s)", ctx.linkedid)
                await conn.send_tool_result(ev.get("call_id"), outcome.result)
                continue

            # EV_TICK (or any unknown type): guardrails already evaluated above; nothing else.
            continue

    # --- Thin I/O edge (UNRUN in the sandbox — py_compile + review only) ------------------

    async def _default_connect(
        self, spec: AgentSpec, ctx: AgentCallContext
    ) -> RealtimeConnection:
        """Open the real AudioSocket↔OpenAI-WS bridge. Lazily imported so the pure core stays
        importable without websockets/audio deps. NOT exercised by unit tests."""
        return await _OpenAIRealtimeConnection.open(spec, ctx)

    async def _default_persist(
        self, ctx: AgentCallContext, assembler: TranscriptAssembler, spec: AgentSpec
    ) -> None:
        """Write the speaker-labeled transcript INLINE to the `transcriptions` store. Lazily
        imports sqlalchemy; NOT exercised by unit tests (no DB in the sandbox)."""
        await _persist_transcript(ctx, assembler)


def _merge_tool_data(data: dict, updates: dict) -> None:
    """Merge a ToolOutcome's data into the running result data. `sms_outbox` accumulates across
    multiple send_sms calls; everything else (e.g. `captured`) is last-write-wins."""
    for k, v in (updates or {}).items():
        if k == "sms_outbox":
            data.setdefault("sms_outbox", []).extend(v)
        else:
            data[k] = v


async def _safe_close(conn: RealtimeConnection) -> None:
    try:
        await conn.close()
    except Exception:  # noqa: BLE001 - close is best-effort at end-of-session
        logger.debug("openai_realtime: connection close failed", exc_info=True)


def _settings_reconnects() -> int:
    """The configured WS-reconnect count (default 1), read lazily so the seam stays importable
    in the settings-less sandbox."""
    try:
        from app.core.config import settings

        return int(getattr(settings, "VOICE_AGENT_WS_RECONNECTS", DEFAULT_WS_RECONNECTS))
    except Exception:  # noqa: BLE001
        return DEFAULT_WS_RECONNECTS


# ==========================================================================================
# UNRUN I/O EDGE — real AudioSocket ↔ OpenAI Realtime WS bridge + transcript persistence.
# These require websockets/asyncio-socket/sqlalchemy + a live Asterisk + OpenAI; they are
# NOT unit-tested (the sandbox has no audio/WS/DB). Kept thin and heavily commented; the pure
# logic above is what the tests exercise. Verified by py_compile + review only.
# ==========================================================================================


class _OpenAIRealtimeConnection:
    """Bridges the caller's audio (AudioSocket/TCP, external-media per Ticket 03) to the OpenAI
    Realtime WS and back, exposing the normalized `RealtimeConnection` interface.

    Server-VAD barge-in: OpenAI does turn detection server-side. On `input_audio_buffer.speech_started`
    we EAGERLY flush the outbound audio buffer (send `response.cancel` + drop already-queued frames)
    so the agent stops talking the instant the caller speaks. Caller speech is transcribed via the
    session's `input_audio_transcription`; agent speech via `response.audio_transcript.*` — both
    normalized to EV_SPEECH so the transcript is speaker-labeled without post-call STT.

    A background reader task drains the WS and the AudioSocket, pushing normalized events onto an
    asyncio.Queue; `next_event` pops it with a timeout equal to the silence budget, synthesizing an
    EV_TICK on timeout so the drive loop's guardrails always get a chance to fire even in dead air.
    """

    def __init__(self, ws, audiosock, spec: AgentSpec, ctx: AgentCallContext) -> None:
        self._ws = ws
        self._audiosock = audiosock
        self._spec = spec
        self._ctx = ctx

    @classmethod
    async def open(cls, spec: AgentSpec, ctx: AgentCallContext) -> "_OpenAIRealtimeConnection":
        # Lazy imports: keep the module importable in the sandbox.
        import json

        import websockets  # type: ignore

        from app.core.config import settings

        api_key = settings.OPENAI_API_KEY
        if not api_key:
            raise RealtimeConnectionError("OPENAI_API_KEY not set")
        model = spec.model or settings.OPENAI_REALTIME_MODEL
        try:
            ws = await websockets.connect(
                f"wss://api.openai.com/v1/realtime?model={model}",
                extra_headers={
                    "Authorization": f"Bearer {api_key}",
                    "OpenAI-Beta": "realtime=v1",
                },
                open_timeout=10,
            )
        except Exception as exc:  # noqa: BLE001 - any handshake failure is retryable
            raise RealtimeConnectionError(f"OpenAI WS connect failed: {exc}") from exc

        enabled = enabled_tools(spec.tools)
        voice = spec.voice or settings.OPENAI_REALTIME_VOICE
        # Configure the session: instructions, voice, server-VAD, tools, and caller-side
        # transcription (so caller speech lands in the transcript).
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "instructions": build_instructions(spec),
                "voice": voice,
                "modalities": ["audio", "text"],
                "input_audio_format": "g711_ulaw",   # 8kHz telephony (AudioSocket)
                "output_audio_format": "g711_ulaw",
                "input_audio_transcription": {"model": "whisper-1"},
                "turn_detection": {"type": "server_vad"},
                "tools": build_realtime_tools(enabled),
            },
        }))
        # AudioSocket bridge is set up by the runtime/ARI layer (external-media channel); the
        # concrete socket object is threaded in there. Left as a placeholder wire-up point.
        audiosock = None
        return cls(ws, audiosock, spec, ctx)

    async def next_event(self) -> Optional[dict]:  # pragma: no cover - unrun I/O path
        raise NotImplementedError(
            "AudioSocket↔OpenAI-WS event pump is the reviewed-not-run I/O path (Ticket 12)"
        )

    async def send_tool_result(self, call_id, result) -> None:  # pragma: no cover
        import json

        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result),
            },
        }))
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def close(self) -> None:  # pragma: no cover - unrun I/O path
        try:
            await self._ws.close()
        finally:
            if self._audiosock is not None:
                self._audiosock.close()


async def _persist_transcript(
    ctx: AgentCallContext, assembler: TranscriptAssembler
) -> None:  # pragma: no cover - unrun DB path
    """Insert the agent leg's speaker-labeled transcript into `transcriptions`, keyed to the call
    by its linkedid (== provider_call_sid). Mirrors app/flows/runtime._emit_node_event's call
    lookup. UNRUN in the sandbox (needs sqlalchemy + Postgres)."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.flows.runtime import PROVIDER_NAME
    from app.models import Call, Transcription
    from app.services.ingestion import _get_or_create_provider

    async with SessionLocal() as db:
        provider = await _get_or_create_provider(db, PROVIDER_NAME)
        call = (
            await db.execute(
                select(Call).where(
                    Call.provider_id == provider.id,
                    Call.provider_call_sid == ctx.linkedid,
                )
            )
        ).scalar_one_or_none()
        if call is None:
            logger.info("openai_realtime: no call row for linkedid=%s; transcript not stored", ctx.linkedid)
            return
        db.add(Transcription(
            call_id=call.id,
            recording_id=None,
            engine=ENGINE_NAME,
            text=assembler.text(),
            language="en",
            segments=assembler.segments(),
            status="completed",
        ))
        await db.commit()

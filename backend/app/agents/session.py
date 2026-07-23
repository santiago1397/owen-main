"""Pluggable voice-agent sessions (Ticket 11) — mirrors app/analysis/transcription.py.

The rest of the platform only sees a `VoiceAgentSession` that, given an `AgentSpec` (a
pinned agent-version's config) and an `AgentCallContext` (the live call), runs a
conversational turn and returns an `AgentResult` = `{port, data}`. WHICH engine produced it
is a config switch that never leaks past this module:

- per-agent `engine` selects the runtime;
- the GLOBAL `settings.VOICE_AGENT_ENGINE` kill-switch, when non-empty, FORCES every agent
  onto that engine (flip to "dummy" to stop all real audio instantly).

The agent NEVER bridges or hangs up — it only returns a PORT; the flow interpreter drives
the graph edge (see app/flows/interpreter.py `_h_ai_agent`). Ports:
  transfer | end_call  -> flow-exit tools (app/agents/tools.py)
  default              -> agent finished with no explicit exit
  failed               -> the session errored (interpreter routes to the `failed` port)

`dummy` is the offline, deterministic default so the node + interpreter + version-pinning
are testable with no real audio. `openai_realtime` (and `vapi`/`diy`) are REGISTERED but
STUBBED — they raise until Ticket 12 fills them in.

Import-light on purpose (stdlib + app.core.config only): no sqlalchemy/httpx/websockets, so
the seam is exercisable in the sandbox. DB glue (resolve+pin the agent version, build the
spec from the ORM row, run the session on a StasisStart) lives in app/flows/runtime.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.agents.tools import enabled_tools, is_valid_port


@dataclass
class AgentSpec:
    """A pinned agent-version's config, flattened for the runtime. Built by the DB glue from
    an `AgentVersion` row; carries everything an engine needs and nothing DB-shaped."""

    agent_id: str
    version_id: str | None = None
    persona: str = ""
    voice: str = ""
    greeting: str = ""
    model: str = ""
    engine: str = "dummy"
    tools: dict = field(default_factory=dict)       # {tool_name: bool} per-agent toggles
    knowledge: str = ""                              # in-context knowledge text
    guardrails: dict = field(default_factory=dict)  # max_call_seconds/max_silence_seconds/model_tier
    config: dict = field(default_factory=dict)       # raw version config (engine-specific extras)


@dataclass
class AgentCallContext:
    """The live call the session runs against. `ari` is present for real engines that drive
    audio; the dummy ignores it (and it's absent in unit tests)."""

    channel_id: str
    linkedid: str
    caller_number: str | None = None
    ari: object | None = None


@dataclass
class AgentResult:
    """A session's outcome. `port` is one of tools.VALID_PORTS; `data` carries any in-call
    tool output (e.g. `data["captured"]` from capture_lead) for downstream attribution."""

    port: str
    data: dict = field(default_factory=dict)


class VoiceAgentSession(Protocol):
    name: str

    async def run(self, spec: AgentSpec, ctx: AgentCallContext) -> AgentResult: ...


class DummyVoiceAgentSession:
    """Deterministic, no-audio session — for local/offline runs and tests.

    Scriptable so the interpreter/pinning can be proven for EVERY exit port without real
    audio: the returned port comes from `spec.config["dummy"]["port"]` (default "end_call").
    If the `capture_lead` tool is toggled on, it simulates a captured lead and surfaces it in
    `data["captured"]` — the wiring onto the existing analysis `captured` path."""

    name = "dummy"

    async def run(self, spec: AgentSpec, ctx: AgentCallContext) -> AgentResult:
        script = spec.config.get("dummy") if isinstance(spec.config.get("dummy"), dict) else {}
        port = script.get("port") or "end_call"
        if not is_valid_port(port):
            port = "end_call"
        data: dict = {}
        tools = enabled_tools(spec.tools)
        if "capture_lead" in tools:
            data["captured"] = script.get("captured") or {
                "name": "Test Caller",
                "intent": "demo",
                "source": "dummy_agent",
            }
        if "send_sms" in tools and script.get("sms"):
            data["sms_sent"] = script["sms"]
        return AgentResult(port=port, data=data)


class _StubVoiceAgentSession:
    """Registered-but-unimplemented engine (filled in by Ticket 12). Raising on `run` (never
    at construction) keeps the registry importable and the kill-switch selectable now."""

    name = "stub"

    def __init__(self, engine_name: str):
        self.name = engine_name

    async def run(self, spec: AgentSpec, ctx: AgentCallContext) -> AgentResult:
        raise NotImplementedError(
            f"voice-agent engine '{self.name}' is not implemented yet (Ticket 12)"
        )


def _make_stub(engine_name: str):
    def factory() -> VoiceAgentSession:
        return _StubVoiceAgentSession(engine_name)

    return factory


def _make_openai_realtime():
    """Factory for the real openai_realtime engine (Ticket 12). Imported LAZILY so this seam
    stays import-light (stdlib only) — app.agents.openai_realtime pulls in nothing heavy at
    module top either, but the lazy import also breaks the session<->openai_realtime cycle."""

    def factory() -> VoiceAgentSession:
        from app.agents.openai_realtime import OpenAIRealtimeSession

        return OpenAIRealtimeSession()

    return factory


# name -> zero-arg factory. `dummy` + `openai_realtime` (Ticket 12) are live; vapi/diy stubbed.
_ENGINES: dict[str, object] = {
    "dummy": DummyVoiceAgentSession,
    "openai_realtime": _make_openai_realtime(),
    "vapi": _make_stub("vapi"),
    "diy": _make_stub("diy"),
}

DEFAULT_ENGINE = "dummy"


def _forced_engine() -> str:
    """The global kill-switch value, or "" if unset/unavailable. Imported LAZILY so the seam
    stays importable in the dependency-light sandbox (settings pulls pydantic-settings)."""
    try:
        from app.core.config import settings

        return (settings.VOICE_AGENT_ENGINE or "").strip()
    except Exception:  # noqa: BLE001 - no settings in the sandbox -> honour per-agent engine
        return ""


def _select_engine(forced: str | None, agent_engine: str | None) -> str:
    """Pure resolver (testable without settings): the kill-switch `forced` wins when non-empty,
    else the agent's own `engine`, else the dummy default."""
    forced = (forced or "").strip()
    if forced:
        return forced
    return (agent_engine or "").strip() or DEFAULT_ENGINE


def select_voice_agent_engine(agent_engine: str | None) -> str:
    """Resolve the engine NAME to run: the global `VOICE_AGENT_ENGINE` kill-switch wins when
    set (non-empty), otherwise the agent's own `engine`, otherwise the dummy default."""
    return _select_engine(_forced_engine(), agent_engine)


def get_voice_agent_session(engine_name: str) -> VoiceAgentSession:
    """Instantiate the session for `engine_name`; unknown names fall back to the dummy so a
    misconfiguration degrades to safe/offline rather than crashing the call."""
    factory = _ENGINES.get(engine_name, DummyVoiceAgentSession)
    return factory()  # type: ignore[operator]


def get_session_for_agent(spec: AgentSpec) -> VoiceAgentSession:
    """Convenience: pick + build the session for a spec, honouring the kill-switch."""
    return get_voice_agent_session(select_voice_agent_engine(spec.engine))

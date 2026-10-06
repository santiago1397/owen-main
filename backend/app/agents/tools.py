"""Fixed voice-agent tool registry with per-agent toggles (Ticket 11).

There is NO arbitrary LLM-driven HTTP: an agent may only invoke tools from this closed
registry, and only the ones its version config toggles ON. Two kinds:

- FLOW_EXIT tools (`transfer`, `end_call`) end the agent turn and hand a PORT back to the
  flow interpreter — the agent NEVER bridges/hangs up itself; the interpreter drives the
  graph edge for that port (see app/flows/interpreter.py `_h_ai_agent`).
- IN_CALL tools (`capture_lead`, `send_sms`) run DURING the session and do not exit the
  node. `capture_lead` produces a structured lead payload that flows out via the session
  result's `data["captured"]` — the seam onto the existing analysis `captured` path
  (persisting it is a later ticket; here we only prove the wiring).

Pure/stdlib-only so it imports in the sandbox with no DB/engine deps.
"""

from __future__ import annotations

# kind constants
FLOW_EXIT = "flow_exit"
IN_CALL = "in_call"

# name -> {kind, exit_port, description}. `exit_port` is set only for FLOW_EXIT tools and is
# the interpreter port the tool maps to (wired to the ai_agent node's `next`).
TOOLS: dict[str, dict] = {
    "transfer": {
        "kind": FLOW_EXIT,
        "exit_port": "transfer",
        "description": "Hand the call back to the flow's `transfer` port (e.g. to a human).",
    },
    "end_call": {
        "kind": FLOW_EXIT,
        "exit_port": "end_call",
        "description": "Politely end the call; the flow takes the `end_call` port.",
    },
    "capture_lead": {
        "kind": IN_CALL,
        "exit_port": None,
        "description": "Record caller-provided lead details (name/intent/etc.) mid-call.",
    },
    "send_sms": {
        "kind": IN_CALL,
        "exit_port": None,
        "description": "Send a follow-up SMS to the caller during the call.",
        # NOT implemented by owen_voice, which is the engine that answers real calls. Its
        # registry has three tools and no send_sms, and `enabled_tools` there ignores names
        # it does not know -- so an agent toggling this on got NO error, NO log line and no
        # SMS. Saying so here is what lets `validate_agent_config` refuse it at activation
        # instead of an operator discovering it from a customer who never got their text.
        "engines": ("openai_realtime", "dummy"),
    },
    "request_change": {
        "kind": IN_CALL,
        "exit_port": None,
        "description": "Pass a caller's request to reschedule, cancel or change something to "
                       "the office as an urgent CRM task (RETELL-PLAN C3). The agent only "
                       "REQUESTS — nothing is moved or cancelled by it.",
        # Retell only (decision 13). owen-voice's registry has no such tool, and adding it
        # there is a separate piece of work; naming the engine here is what makes activation
        # refuse it on owen_voice instead of it being silently dropped there.
        "engines": ("retell",),
    },
}

# The tools a Retell agent may have (RETELL-PLAN C1). Retell owns the conversation, so these
# are the only things OWEN does on its behalf; anything else — above all `send_sms` — is
# refused at activation.
RETELL_TOOLS: frozenset[str] = frozenset({"transfer", "end_call", "capture_lead",
                                          "request_change"})

# Tools with no `engines` key run everywhere. Only a tool that SOME engine cannot honour
# needs to name the ones that can.
ALL_ENGINES = "*"


def engines_for(name: str) -> tuple[str, ...] | str:
    """Which engines implement `name`, or ALL_ENGINES."""
    return TOOLS.get(name, {}).get("engines", ALL_ENGINES)


def unsupported_tools(toggles: dict | None, engine: str) -> list[str]:
    """Toggled-on tools this engine does not implement, in registry order.

    The check that stops a capability being silently dropped between two services that
    each hold their own copy of the registry.
    """
    toggles = toggles or {}
    out = []
    for name in TOOLS:
        if not toggles.get(name):
            continue
        engines = engines_for(name)
        if engines != ALL_ENGINES and engine not in engines:
            out.append(name)
    return out

# The ports a session may return. `default` / `failed` are interpreter-level (not tools):
# `default` = the agent finished with no explicit exit tool; `failed` = the session errored.
FLOW_EXIT_PORTS: frozenset[str] = frozenset(
    t["exit_port"] for t in TOOLS.values() if t["kind"] == FLOW_EXIT
)
VALID_PORTS: frozenset[str] = FLOW_EXIT_PORTS | frozenset({"default", "failed"})


def enabled_tools(toggles: dict | None) -> dict[str, dict]:
    """The subset of TOOLS toggled ON for an agent version (`{name: True}` in its config).

    Unknown names are ignored (the registry is the source of truth), so a stale toggle can
    never smuggle in a tool the platform doesn't implement."""
    toggles = toggles or {}
    return {name: spec for name, spec in TOOLS.items() if toggles.get(name)}


def is_valid_port(port: str | None) -> bool:
    return port in VALID_PORTS

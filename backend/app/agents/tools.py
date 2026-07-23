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
    },
}

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

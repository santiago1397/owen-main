"""The closed tool registry, mirroring backend app/agents/tools.py.

Same principle, restated where the LLM actually runs: an agent may only invoke tools from
this fixed set, and only the ones its version config toggles ON. The model chooses WHICH
declared tool, never what it does.

FLOW_EXIT tools end the turn and hand a PORT back to the flow interpreter — the agent never
bridges or hangs up on anyone. IN_CALL tools run mid-conversation and the agent keeps talking.
"""

from __future__ import annotations

FLOW_EXIT = "flow_exit"
IN_CALL = "in_call"

TOOLS: dict[str, dict] = {
    "transfer": {
        "kind": FLOW_EXIT, "exit_port": "transfer",
        "description": "Hand the call to a human. Use when the caller asks for a person, "
                       "is upset, or needs something you cannot do.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "Why you are transferring."}}},
    },
    "end_call": {
        "kind": FLOW_EXIT, "exit_port": "end_call",
        "description": "Politely end the call once the caller's need is met.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string"}}},
    },
    "capture_lead": {
        "kind": IN_CALL, "exit_port": None,
        "description": "Record what you have learned about the caller. Call this AS SOON AS "
                       "you have any of these details -- do not wait until the end of the "
                       "call, because a caller who hangs up early still leaves a usable lead.",
        # The shared-core vocabulary from AI_AGENT_SPEC D7: the same keys across every agent,
        # so cross-agent reporting stays possible.
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "phone": {"type": "string"},
            "email": {"type": "string"},
            "address": {"type": "string", "description": "Service address."},
            "intent": {"type": "string", "description": "What they want, in a few words."},
            "urgency": {"type": "string", "description": "emergency | soon | routine"},
            "notes": {"type": "string"},
        }},
    },
}


def enabled_tools(toggles: dict | None) -> dict:
    """The subset toggled ON for this agent version. Unknown names are ignored, so a stale
    toggle can never smuggle in a capability the platform does not implement."""
    toggles = toggles or {}
    return {n: t for n, t in TOOLS.items() if toggles.get(n)}


def openai_schema(enabled: dict, transfer_targets: dict | None = None) -> list:
    """The `tools` array for an OpenAI-compatible chat completion.

    `transfer` is specialised against the agent's declared ALLOWLIST (AI_AGENT_SPEC D9): the
    destination becomes an enum of names the operator wrote down, so the model picks WHICH
    declared destination and can never name a number. That is the same property as the custom
    tools in D6, and it is what stops an LLM being talked into dialling a premium-rate number
    over the trunk. With no allowlist configured the tool keeps its plain form and the flow's
    own `transfer` edge decides, exactly as before.
    """
    out = []
    names = sorted(transfer_targets or {})
    for name, spec in enabled.items():
        params = spec["parameters"]
        if name == "transfer" and names:
            params = {
                "type": "object",
                "properties": {
                    "destination": {
                        "type": "string",
                        "enum": names,
                        "description": "Where to send the caller. Choose the closest match.",
                    },
                    "reason": {"type": "string"},
                },
                "required": ["destination"],
            }
        out.append({"type": "function", "function": {
            "name": name, "description": spec["description"], "parameters": params,
        }})
    return out

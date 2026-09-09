"""Append-only versioning + pure spec-building for AI voice agents (Ticket 11).

Mirrors app/flows/service.py: `agent_versions` are immutable by construction — saving an
agent NEVER mutates an existing version row, it INSERTs a new one whose `version` is one
past the current max. `next_version_number` is that pure kernel (re-exported from the flows
service; the rule is identical, so there is one implementation).

`build_spec` turns a stored version-config dict into the flat `AgentSpec` the seam consumes.
Kept pure (no DB/ORM import) so it is unit-testable and reusable from the runtime glue.
"""

from __future__ import annotations

from app.agents.session import AgentSpec, _ENGINES
from app.agents.tools import TOOLS
from app.flows.service import next_version_number

__all__ = ["next_version_number", "build_spec", "validate_agent_config",
           "KNOWLEDGE_MAX_CHARS", "voice_warning"]

# VOICE_STACK_MIGRATION M11. `knowledge` is concatenated WHOLE into the system prompt by a
# property recomputed EVERY TURN, and until this cap it was the one completely unbounded input
# in the loop -- nothing truncated it in the UI, the schema, the spec, the wire or the prompt.
# A 50 KB blob is ~12k tokens re-billed per turn, silently. The caller-context path solved the
# identical problem with MAX_SUMMARY_CHARS/MAX_FACTS; this is knowledge's version of that.
#
# 6000 is a STARTING LINE, not a finding. Hitting it is the signal that one agent may no longer
# be the right shape (D12 agent_slots is built and unused) -- with real calls to argue it.
KNOWLEDGE_MAX_CHARS = 6000

# Per-provider voice vocabularies (M5). Deepgram ships ~49 English Aura-2 voices and adds more,
# so it is validated by PREFIX rather than by an enumeration that would be stale within a month.
_OPENAI_VOICES = frozenset({
    "alloy", "ash", "ballad", "coral", "echo", "fable",
    "nova", "onyx", "sage", "shimmer", "verse",
})


def _capped_knowledge(raw, agent_id: str = "") -> str:
    text = str(raw or "")
    if len(text) <= KNOWLEDGE_MAX_CHARS:
        return text
    import logging

    logging.getLogger("agents.service").warning(
        "agent %s: knowledge is %d chars, truncating to %d for the prompt. This version was "
        "activated before the cap existed; re-author it rather than relying on the truncation.",
        agent_id or "?", len(text), KNOWLEDGE_MAX_CHARS,
    )
    return text[:KNOWLEDGE_MAX_CHARS]


def voice_warning(provider: str, voice: str) -> str | None:
    """A wrong voice is a WARNING, never an error (M5).

    Changing TTS vendor invalidates every stored voice string at once. Refusing to activate
    agents that were fine yesterday is a worse outcome than one call in the default voice, so
    an unknown voice resolves to the provider default and says so. Returns None when fine.
    """
    voice = str(voice or "").strip()
    if not voice:
        return None
    provider = str(provider or "").strip().lower()
    if provider == "deepgram":
        if not voice.startswith("aura-"):
            return (f"voice '{voice}' is not a Deepgram voice (expected aura-2-<name>-en); "
                    "the provider default will be used")
    elif provider in ("openai", ""):
        if voice not in _OPENAI_VOICES:
            return (f"voice '{voice}' is not an OpenAI voice; the provider default will be "
                    "used")
    return None


def validate_agent_config(config: dict | None) -> tuple[list[str], list[str]]:
    """Pure activation gate for an agent version (mirrors app.flows.validate_graph): returns
    (errors, warnings). Hard errors block activation; warnings never do. Saving a draft never
    runs this — like flow_versions, drafts save freely and validation gates activation only."""
    errors: list[str] = []
    warnings: list[str] = []
    cfg = config if isinstance(config, dict) else {}

    engine = str(cfg.get("engine") or "dummy")
    if engine not in _ENGINES:
        errors.append(f"unknown engine '{engine}' (known: {', '.join(sorted(_ENGINES))})")

    tools = cfg.get("tools") if isinstance(cfg.get("tools"), dict) else {}
    for name in tools:
        if name not in TOOLS:
            errors.append(f"unknown tool '{name}' (not in the fixed tool registry)")

    # Custom HTTP tools (AI_AGENT_SPEC D6). Validated at activation so an operator is
    # told their tool is broken here, rather than it failing in front of a caller.
    try:
        from app.flows.custom_tools import validate_custom_tools

        errors.extend(validate_custom_tools(cfg.get("custom_tools")))
    except Exception:  # noqa: BLE001 - validation must never block on an import
        pass

    # Caller-context provider (CRM_CONTEXT_SPEC C14).
    try:
        from app.agents.context import validate_provider

        errors.extend(validate_provider(cfg.get("context_provider")))
    except Exception:  # noqa: BLE001
        pass

    # Transfer allowlist (AI_AGENT_SPEC D9). Validated at ACTIVATION, because a malformed
    # entry means the agent silently cannot reach a destination the operator believes it can.
    targets = cfg.get("transfer_targets")
    if targets is not None:
        if not isinstance(targets, dict):
            errors.append("transfer_targets must be an object of {name: {kind, target}}")
        else:
            for name, entry in targets.items():
                if not isinstance(entry, dict):
                    errors.append(f"transfer target '{name}' must be an object")
                    continue
                kind = str(entry.get("kind") or "number")
                if kind not in ("number", "operator", "flow", "agent"):
                    errors.append(
                        f"transfer target '{name}' has unknown kind '{kind}' "
                        "(number | operator | flow | agent)"
                    )
                if not str(entry.get("target") or "").strip():
                    errors.append(f"transfer target '{name}' has no target")
        if isinstance(targets, dict) and targets and not (cfg.get("tools") or {}).get("transfer"):
            warnings.append(
                "transfer_targets are declared but the `transfer` tool is toggled off — "
                "the agent can never reach them"
            )

    # Knowledge budget (M11). A HARD error, because this is exactly what activation validation
    # is for: the operator learns while editing, not when the bill arrives. Every turn of every
    # call pays for this text.
    knowledge = str(cfg.get("knowledge") or "")
    if len(knowledge) > KNOWLEDGE_MAX_CHARS:
        errors.append(
            f"knowledge is {len(knowledge)} characters, over the {KNOWLEDGE_MAX_CHARS} limit. "
            "It is re-sent to the model on EVERY turn, so this is paid for repeatedly. Move "
            "per-customer detail into a tool, or split the agent."
        )

    # Voice (M5) -- warning only, so a provider switch never bricks an existing agent.
    warn = voice_warning(str(cfg.get("tts_provider") or ""), cfg.get("voice"))
    if warn:
        warnings.append(warn)

    if not str(cfg.get("greeting") or "").strip():
        warnings.append("no greeting set — the agent will open with nothing scripted")
    if not str(cfg.get("persona") or "").strip():
        warnings.append("no persona set — the agent has no described behaviour")
    return errors, warnings


def build_spec(agent_id: str, version_id: str | None, config: dict | None) -> AgentSpec:
    """Flatten an agent-version `config` dict into an `AgentSpec`.

    `config` is the JSON stored on the version row (persona/voice/greeting/model/engine/
    tools/knowledge/guardrails + any engine-specific extras). Missing keys default safely so
    a partially-authored draft still yields a runnable (dummy) spec."""
    cfg = config or {}
    return AgentSpec(
        agent_id=str(agent_id),
        version_id=str(version_id) if version_id is not None else None,
        persona=str(cfg.get("persona") or ""),
        voice=str(cfg.get("voice") or ""),
        greeting=str(cfg.get("greeting") or ""),
        model=str(cfg.get("model") or ""),
        engine=str(cfg.get("engine") or "dummy"),
        tools=cfg.get("tools") if isinstance(cfg.get("tools"), dict) else {},
        # Truncated even though activation already rejects an over-budget draft: versions
        # activated BEFORE the cap existed are immutable and still runnable, and one of those
        # would otherwise re-bill an unbounded prompt on every turn forever. Loud, not silent
        # -- the log names the agent so it can be re-authored rather than quietly degraded.
        knowledge=_capped_knowledge(cfg.get("knowledge"), agent_id),
        guardrails=cfg.get("guardrails") if isinstance(cfg.get("guardrails"), dict) else {},
        config=cfg,
    )

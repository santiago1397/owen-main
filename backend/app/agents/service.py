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

__all__ = ["next_version_number", "build_spec", "validate_agent_config"]


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
        knowledge=str(cfg.get("knowledge") or ""),
        guardrails=cfg.get("guardrails") if isinstance(cfg.get("guardrails"), dict) else {},
        config=cfg,
    )

"""Transfer-destination resolution — PURE, stdlib only (AI_AGENT_SPEC D9).

Split out of app/flows/runtime.py for the reason every pure kernel in this codebase is split
out (interpreter, validator, variables, billing): runtime imports httpx and sqlalchemy, so
anything living there cannot be exercised without them — and this is the function carrying the
security property, which is precisely the one that should be testable in a bare sandbox.

THE PROPERTY: the model chooses WHICH declared destination, never what number to dial. An LLM
able to dial arbitrary numbers over the BulkVS trunk is a toll-fraud primitive — the attack is
a phone call, where someone spends two minutes being persuasive and gets the agent to
"transfer me to my colleague" at a premium-rate or international number. Prompt engineering is
not a control against that. The allowlist is.
"""

from __future__ import annotations

from typing import Optional

TRANSFER_KINDS = ("number", "operator", "flow", "agent")


def resolve_transfer_target(targets, name: str) -> Optional[dict]:
    """Look a destination NAME up in an agent version's declared allowlist.

    Returns `{kind, target, name}` or None. None means "not permitted", and the caller falls
    back to the flow's own `transfer` edge — so a bad or absent name degrades to the
    operator's wiring rather than to an arbitrary dial.
    """
    if not name or not isinstance(targets, dict):
        return None
    entry = targets.get(str(name))
    if not isinstance(entry, dict):
        return None
    kind = str(entry.get("kind") or "number")
    target = str(entry.get("target") or "").strip()
    if kind not in TRANSFER_KINDS or not target:
        return None
    return {"kind": kind, "target": target, "name": str(name)}


def target_names(targets) -> list:
    """The destination names an agent may choose from, for the tool schema. Only well-formed
    entries are offered: a malformed one is unreachable, so advertising it would invite the
    model to pick something that silently cannot work."""
    if not isinstance(targets, dict):
        return []
    return sorted(n for n in targets if resolve_transfer_target(targets, n) is not None)

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

# How long a `number` / `operator` target rings before the transfer counts as unanswered
# (2026-10-08, owner). Optional per target, `"ring_seconds": 12`; absent = the platform's
# OPERATOR_RING_TIMEOUT_SECONDS. It exists because a target can have its OWN no-answer rule:
# the office's Quo line forwards to another AI number after 15 s, so a 25 s ring was answered
# by the wrong agent. Ringing for less than the target's own rule gives up first.
RING_SECONDS_MIN = 5
RING_SECONDS_MAX = 60
RING_KINDS = ("number", "operator")


def ring_seconds_problem(name: str, value) -> Optional[str]:
    """A sentence saying why `value` is not a usable `ring_seconds`, or None if it is."""
    if isinstance(value, bool) or not isinstance(value, int):
        return (f"transfer target '{name}' has ring_seconds {value!r}: it must be a whole "
                f"number of seconds from {RING_SECONDS_MIN} to {RING_SECONDS_MAX}")
    if not RING_SECONDS_MIN <= value <= RING_SECONDS_MAX:
        return (f"transfer target '{name}' rings for {value} seconds: ring_seconds must be "
                f"from {RING_SECONDS_MIN} to {RING_SECONDS_MAX}")
    return None


# A target may declare up to THEN_MAX follow-up numbers to ring, in order, when it is not
# answered (2026-10-19, owner): "12 s to the main line, then 15 s to another number". Each step
# is `{"kind": "number"|"operator", "target": ..., "ring_seconds": N}`; only ringing kinds.
THEN_MAX = 2


def then_problems(name: str, then) -> list:
    """Sentences saying why a target's `then` list is unusable (empty list = fine)."""
    if then is None:
        return []
    if not isinstance(then, list) or not then:
        return [f"transfer target '{name}' has a `then` that is not a non-empty list"]
    if len(then) > THEN_MAX:
        return [f"transfer target '{name}' rings at most {THEN_MAX} more numbers after the first"]
    out = []
    for i, step in enumerate(then, 1):
        label = f"{name} (then #{i})"
        if not isinstance(step, dict):
            out.append(f"transfer target '{label}' must be an object"); continue
        if str(step.get("kind") or "number") not in RING_KINDS:
            out.append(f"transfer target '{label}' must be a number or operator (it rings)")
        if not str(step.get("target") or "").strip():
            out.append(f"transfer target '{label}' has no target")
        if "ring_seconds" in step:
            problem = ring_seconds_problem(label, step.get("ring_seconds"))
            if problem:
                out.append(problem)
    return out


def resolve_transfer_target(targets, name: str) -> Optional[dict]:
    """Look a destination NAME up in an agent version's declared allowlist.

    Returns `{kind, target, name}` (plus `ring_seconds` when the entry declares a valid one)
    or None. None means "not permitted", and the caller falls
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
    chosen = {"kind": kind, "target": target, "name": str(name)}
    ring = entry.get("ring_seconds")
    if ring is not None and ring_seconds_problem(str(name), ring) is None:
        # An invalid value is refused at activation (agents/service.py); one that slipped
        # through is ignored here, so the platform default rings rather than nothing.
        chosen["ring_seconds"] = ring
    then = entry.get("then")
    if kind in RING_KINDS and then is not None and not then_problems(str(name), then):
        chosen["then"] = [{"kind": str(st.get("kind") or "number"),
                           "target": str(st.get("target")).strip(),
                           **({"ring_seconds": st["ring_seconds"]} if "ring_seconds" in st else {})}
                          for st in then]
    return chosen


def target_names(targets) -> list:
    """The destination names an agent may choose from, for the tool schema. Only well-formed
    entries are offered: a malformed one is unreachable, so advertising it would invite the
    model to pick something that silently cannot work."""
    if not isinstance(targets, dict):
        return []
    return sorted(n for n in targets if resolve_transfer_target(targets, n) is not None)

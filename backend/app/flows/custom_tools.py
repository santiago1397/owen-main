"""Per-agent custom HTTP tool DECLARATIONS — pure validation (AI_AGENT_SPEC D6).

The backend half: it validates declarations at ACTIVATION so an operator learns their
tool is broken while editing, not while a caller is on the line. Execution lives in
owen-voice (D13: tools run where the latency budget is, not a hop away), which carries
its own copy of the same rules.

The closed registry in app/tools.py stays for PLATFORM actions. This is the other half: an
agent version may declare its own HTTP tools, so integrating a new system is a config change
rather than a code change and a deploy.

WHY THIS DOES NOT VIOLATE THE "no arbitrary LLM-driven HTTP" rule the platform states: the
property being protected is that *the LLM cannot choose a URL*. It still cannot. The URL set
is fixed in an immutable, activation-validated, version-pinned config written by an operator;
the model only ever chooses WHICH declared tool to invoke. The allowlist moved from Python
source into versioned data — the guarantee did not move at all.

SYNC vs ASYNC is the other half of the design, and it is a latency decision:

    sync   ~800ms hard, with a filler phrase. FAST READS ONLY. The caller is sitting in
           silence while it runs, and 300-400ms is already enough to break the feel of a
           live call. The flow `request` node's 5s ceiling is correct BETWEEN prompts and
           catastrophic mid-conversation — five seconds of silence reads as a dropped call.
    async  returns a receipt immediately and completes in the background. Writes, slow
           lookups and third-party calls belong here: a lead being saved must never make a
           caller wait.

Writes default to async for exactly that reason.
"""

from __future__ import annotations

from typing import Optional

# Hard ceiling for a SYNC tool. Not a suggestion — past this the caller thinks the line died.
SYNC_BUDGET_S = 0.8
# What the agent says while a sync tool runs, so the silence is explained rather than dead.
DEFAULT_FILLER = "Let me check that for you."

ALLOWED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
ALLOWED_MODES = ("sync", "async")


def normalise(raw) -> list[dict]:
    """Validated, normalised custom-tool declarations. Malformed entries are DROPPED rather
    than half-configured: a tool that cannot work should be invisible to the model, not
    offered and then failing mid-call."""
    out: list[dict] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("url") or "").strip()
        if not name or not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            continue
        method = str(item.get("method") or "GET").upper()
        if method not in ALLOWED_METHODS:
            method = "GET"
        mode = str(item.get("mode") or "").lower()
        if mode not in ALLOWED_MODES:
            # Writes default to ASYNC: a caller must never wait on one.
            mode = "sync" if method == "GET" else "async"
        params = item.get("parameters")
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
        out.append({
            "name": name,
            "description": str(item.get("description") or "")[:400],
            "url": url,
            "method": method,
            "mode": mode,
            "headers": item.get("headers") if isinstance(item.get("headers"), dict) else {},
            "parameters": params,
            "filler": str(item.get("filler") or DEFAULT_FILLER),
        })
    return out


def openai_schema(tools: list[dict]) -> list:
    return [
        {"type": "function", "function": {
            "name": t["name"],
            "description": t["description"] or f"Call the {t['name']} system.",
            "parameters": t["parameters"],
        }}
        for t in tools
    ]


def find(tools: list[dict], name: str) -> Optional[dict]:
    for t in tools:
        if t["name"] == name:
            return t
    return None


def validate_custom_tools(raw) -> list[str]:
    """Activation-time errors. Returns a list of human-readable problems (empty = fine).

    Separate from `normalise`, which silently drops the unusable: at ACTIVATION an operator
    should be told their tool is broken, while at CALL TIME a broken tool must simply not
    exist rather than fail in front of a caller."""
    errors: list[str] = []
    if raw is None:
        return errors
    if not isinstance(raw, (list, tuple)):
        return ["custom_tools must be a list"]
    seen = set()
    for i, item in enumerate(raw):
        where = f"custom_tools[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object")
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            errors.append(f"{where} has no name")
        elif name in seen:
            errors.append(f"{where}: duplicate tool name '{name}'")
        else:
            seen.add(name)
        url = str(item.get("url") or "").strip()
        if not url:
            errors.append(f"{where} ('{name}') has no url")
        elif not url.lower().startswith(("http://", "https://")):
            errors.append(f"{where} ('{name}') url must be http(s)")
        method = str(item.get("method") or "GET").upper()
        if method not in ALLOWED_METHODS:
            errors.append(f"{where} ('{name}') has unsupported method '{method}'")
        mode = str(item.get("mode") or "").lower()
        if mode and mode not in ALLOWED_MODES:
            errors.append(f"{where} ('{name}') mode must be sync or async")
        if mode == "sync" and method != "GET":
            errors.append(
                f"{where} ('{name}') is a sync {method}: a write must not make a caller "
                "wait — use mode 'async'"
            )
    return errors

"""The body OWEN posts to owen-voice must be a body owen-voice actually reads.

This exists because of a bug that cost a whole feature and produced no error anywhere.

`SessionIn` declares `context` and `context_provider` at the TOP level of the session body.
OWEN sent them nested inside `agent`, where `AgentConfig` does not declare them — so
pydantic dropped both, on every call, in silence. owen-voice therefore had no provider to
call and no local facts to render, and an AI agent never knew who was calling no matter what
`context_provider` was set to. The caller-context feature (CRM_CONTEXT_SPEC, and the CRM's
phase 2a on top of it) was built, deployed and inert.

Nothing failed loudly because nothing was wrong at either end on its own: OWEN sent a
well-formed body, owen-voice accepted it, and pydantic's default is to ignore what it does
not declare. Only the two ends TOGETHER were wrong.

So this test reads owen-voice's models as SOURCE (they live in a separate service with its
own `app` package, not importable from here) and checks the payload against them:

  * every top-level key OWEN sends is declared on `SessionIn`;
  * every key inside `agent` is declared on `AgentConfig`;
  * `context` and `context_provider` are sent at the TOP level specifically — the exact
    regression, named, so it cannot come back quietly.

Run:  python -m tests.test_session_payload_contract      (from backend/)
"""

import ast
import pathlib
import sys

sys.path.insert(0, ".")

VOICE_API = (pathlib.Path(__file__).resolve().parents[2]
             / "owen-voice" / "app" / "agent_api.py")
REMOTE = pathlib.Path(__file__).resolve().parents[1] / "app" / "agents" / "remote.py"

_checks = 0
_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    global _checks
    _checks += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        _failures.append(label)


def declared_fields(source: str, class_name: str) -> set[str]:
    """The pydantic field names on a class, read from source."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {stmt.target.id for stmt in node.body
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)}
    raise AssertionError("no class %r in %s" % (class_name, VOICE_API))


def payload_keys() -> tuple[set[str], set[str]]:
    """(top-level keys, agent keys) of the dict literal assigned to `payload` in remote.py."""
    tree = ast.parse(REMOTE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and node.targets
                and getattr(node.targets[0], "id", "") == "payload"
                and isinstance(node.value, ast.Dict)):
            top = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
            agent: set[str] = set()
            for key, value in zip(node.value.keys, node.value.values):
                if getattr(key, "value", None) == "agent" and isinstance(value, ast.Dict):
                    agent = {k.value for k in value.keys if isinstance(k, ast.Constant)}
            return top, agent
    raise AssertionError("no `payload = {...}` literal in %s" % REMOTE)


def test_owen_voice_declares_everything_owen_sends():
    source = VOICE_API.read_text(encoding="utf-8")
    session_in = declared_fields(source, "SessionIn")
    agent_config = declared_fields(source, "AgentConfig")
    top, agent = payload_keys()

    print("  SessionIn declares  :", ", ".join(sorted(session_in)))
    print("  OWEN sends (top)    :", ", ".join(sorted(top)))

    undeclared_top = top - session_in
    check("every top-level key OWEN sends is declared on SessionIn "
          f"(dropped silently otherwise: {sorted(undeclared_top)})", not undeclared_top)

    undeclared_agent = agent - agent_config
    check("every agent key OWEN sends is declared on AgentConfig "
          f"(dropped silently otherwise: {sorted(undeclared_agent)})", not undeclared_agent)


def test_the_caller_context_goes_where_owen_voice_reads_it():
    top, agent = payload_keys()
    check("`context` is sent at the TOP level", "context" in top)
    check("`context_provider` is sent at the TOP level", "context_provider" in top)
    # The regression itself: inside `agent` they are accepted by nobody and silently lost.
    check("`context` is NOT buried inside `agent`", "context" not in agent)
    check("`context_provider` is NOT buried inside `agent`", "context_provider" not in agent)


def test_the_session_still_carries_what_a_call_needs():
    top, agent = payload_keys()
    for key in ("channel_id", "linkedid", "caller_number", "agent"):
        check(f"the body still carries {key!r}", key in top)
    for key in ("persona", "greeting", "voice", "model", "tools"):
        check(f"the agent config still carries {key!r}", key in agent)


if __name__ == "__main__":
    test_owen_voice_declares_everything_owen_sends()
    test_the_caller_context_goes_where_owen_voice_reads_it()
    test_the_session_still_carries_what_a_call_needs()
    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    for f in _failures:
        print(f"  FAILED: {f}")
    raise SystemExit(1 if _failures else 0)

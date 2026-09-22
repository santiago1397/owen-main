"""The two tool registries must agree, or a capability disappears in silence.

There are two copies of the closed tool registry, and there have to be: `backend/app/
agents/tools.py` validates and describes, `owen-voice/app/tools.py` builds the schema where
the model actually runs. They are two services, two images, no shared package.

The failure mode that makes this file worth having: **`send_sms` was toggleable in the
backend and did not exist in owen-voice.** `enabled_tools` on both sides ignores names it
does not know — correct for a stale toggle, wrong for a real capability — so an operator
could switch on "text the caller during the call", get no error, no warning and no log
line, and no text would ever be sent. Nothing in either service was wrong on its own. The
bug lived in the gap between them, which is exactly where no unit test was looking.

So this test reads owen-voice's registry with `ast` (the way test_crm_event_payload reads
the CRM's source: another repo, another virtualenv, not importable from here) and pins:

  * every tool owen-voice implements is declared in the backend registry;
  * every backend tool NOT restricted by `engines` exists in owen-voice;
  * a tool the backend restricts is exactly one owen-voice does not have — a restriction
    that does not correspond to a real gap is a lie that will rot;
  * `kind` and `exit_port` agree for every shared tool, since the interpreter routes on the
    port owen-voice returns.

Run:  python -m tests.test_tool_registry_contract      (from backend/)
"""

import ast
import pathlib
import sys

sys.path.insert(0, ".")

from app.agents.service import validate_agent_config  # noqa: E402
from app.agents.tools import ALL_ENGINES, TOOLS, engines_for, unsupported_tools  # noqa: E402

VOICE_TOOLS = pathlib.Path(__file__).resolve().parents[2] / "owen-voice" / "app" / "tools.py"

_checks = 0
_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    global _checks
    _checks += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        _failures.append(label)


def _voice_registry() -> dict:
    """owen-voice's TOOLS dict, read as source. No import: different service, different deps."""
    tree = ast.parse(VOICE_TOOLS.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "TOOLS":
            value = node.value
        elif (isinstance(node, ast.Assign) and node.targets
              and getattr(node.targets[0], "id", "") == "TOOLS"):
            value = node.value
        else:
            continue
        out = {}
        for key, val in zip(value.keys, value.values):
            spec = {}
            for k, v in zip(val.keys, val.values):
                name = k.value
                if name in ("kind", "exit_port", "description"):
                    # `kind` is a NAME (FLOW_EXIT / IN_CALL), the others are literals.
                    spec[name] = (v.id.lower() if isinstance(v, ast.Name)
                                  else getattr(v, "value", None))
            out[key.value] = spec
        return out
    raise AssertionError(f"no TOOLS assignment found in {VOICE_TOOLS}")


def test_owen_voice_implements_nothing_the_backend_has_not_declared():
    voice = _voice_registry()
    check(f"owen-voice's registry was parsed ({len(voice)} tools: {', '.join(sorted(voice))})",
          len(voice) >= 3)
    for name in voice:
        check(f"owen-voice's '{name}' is declared in the backend registry", name in TOOLS)


def test_every_unrestricted_backend_tool_exists_in_owen_voice():
    voice = _voice_registry()
    for name in TOOLS:
        if engines_for(name) is ALL_ENGINES or engines_for(name) == ALL_ENGINES:
            check(f"'{name}' claims every engine, so owen-voice must implement it",
                  name in voice)


def test_a_restriction_names_a_real_gap():
    # The other direction, and the one that keeps this honest: if a tool is marked as
    # unsupported by owen_voice, owen-voice really must not have it. Otherwise the
    # restriction is stale and blocks an activation for no reason.
    voice = _voice_registry()
    for name in TOOLS:
        engines = engines_for(name)
        if engines != ALL_ENGINES and "owen_voice" not in engines:
            check(f"'{name}' is restricted away from owen_voice, and owen-voice indeed "
                  f"does not implement it", name not in voice)


def test_kind_and_exit_port_agree():
    voice = _voice_registry()
    for name, spec in voice.items():
        if name not in TOOLS:
            continue
        check(f"'{name}' has the same kind on both sides",
              spec.get("kind") == TOOLS[name]["kind"])
        check(f"'{name}' maps to the same exit port on both sides",
              spec.get("exit_port") == TOOLS[name]["exit_port"])


def test_activation_refuses_a_tool_the_engine_cannot_honour():
    errors, _ = validate_agent_config({"engine": "owen_voice", "tools": {"send_sms": True}})
    check("activating an owen_voice agent with send_sms is refused",
          any("send_sms" in e and "owen_voice" in e for e in errors))
    check("...and the refusal says which engines DO implement it",
          any("openai_realtime" in e for e in errors))

    errors, _ = validate_agent_config(
        {"engine": "owen_voice", "tools": {"capture_lead": True, "transfer": True}})
    check("the tools owen-voice does implement activate cleanly", errors == [])

    errors, _ = validate_agent_config({"engine": "openai_realtime", "tools": {"send_sms": True}})
    check("send_sms is fine on the engine that implements it", errors == [])

    errors, _ = validate_agent_config({"engine": "owen_voice", "tools": {"send_sms": False}})
    check("a tool toggled OFF is not refused", errors == [])


def test_unsupported_tools_is_registry_ordered_and_ignores_unknowns():
    check("an unknown toggle is not reported as unsupported (the registry is the truth)",
          unsupported_tools({"no_such_tool": True}, "owen_voice") == [])
    check("nothing toggled means nothing unsupported",
          unsupported_tools({}, "owen_voice") == [])
    check("send_sms is reported for owen_voice",
          unsupported_tools({"send_sms": True}, "owen_voice") == ["send_sms"])


if __name__ == "__main__":
    test_owen_voice_implements_nothing_the_backend_has_not_declared()
    test_every_unrestricted_backend_tool_exists_in_owen_voice()
    test_a_restriction_names_a_real_gap()
    test_kind_and_exit_port_agree()
    test_activation_refuses_a_tool_the_engine_cannot_honour()
    test_unsupported_tools_is_registry_ordered_and_ignores_unknowns()
    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    for f in _failures:
        print(f"  FAILED: {f}")
    raise SystemExit(1 if _failures else 0)

"""Transfer allowlist + agent slots (AI_AGENT_SPEC D9/D12). Pure — no DB, no ARI.

The property under test is the security one: the model chooses WHICH declared destination,
never what number to dial. An LLM that can dial arbitrary numbers over the trunk is a
toll-fraud primitive, and the allowlist — not the prompt — is the control.

Run:  python -m tests.test_transfer_allowlist      (from backend/)
"""

import sys

sys.path.insert(0, ".")

from app.flows.interpreter import (  # noqa: E402
    PORT_TAKEN_OVER,
    PORT_TRANSFERRED,
    STAND_DOWN_PORTS,
)

_checks = 0


def check(cond, label):
    global _checks
    _checks += 1
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    print(f"  ok  {label}")


from app.flows.transfer import resolve_transfer_target, target_names  # noqa: E402


def _resolve(targets, name):
    return resolve_transfer_target(targets, name)


TARGETS = {
    "sales": {"kind": "number", "target": "+13055551234"},
    "dispatch": {"kind": "operator", "target": "jane@example.com"},
    "spanish": {"kind": "flow", "target": "+19545550000"},
}


def test_declared_names_resolve():
    check(_resolve(TARGETS, "sales")["target"] == "+13055551234", "a declared name resolves")
    check(_resolve(TARGETS, "dispatch")["kind"] == "operator", "kind is carried through")
    check(_resolve(TARGETS, "spanish")["kind"] == "flow", "flow targets are allowed")


def test_undeclared_is_refused():
    """The whole point: anything not written down by an operator is unreachable."""
    check(_resolve(TARGETS, "premium-rate") is None, "an undeclared name resolves to nothing")
    check(_resolve(TARGETS, "+8815551234") is None,
          "a raw NUMBER is not a destination — it can only ever be a declared name")
    check(_resolve(TARGETS, "") is None, "an empty name resolves to nothing")
    check(_resolve(None, "sales") is None, "no allowlist means no destinations at all")


def test_malformed_entries_are_refused():
    check(_resolve({"a": {"kind": "number"}}, "a") is None, "an entry with no target is refused")
    check(_resolve({"a": {"kind": "wormhole", "target": "x"}}, "a") is None,
          "an unknown kind is refused")
    check(_resolve({"a": "just-a-string"}, "a") is None, "a non-object entry is refused")


def test_stand_down_ports():
    check(PORT_TRANSFERRED in STAND_DOWN_PORTS, "transferred stands the interpreter down")
    check(PORT_TAKEN_OVER in STAND_DOWN_PORTS, "taken_over stands the interpreter down")
    check("transfer" not in STAND_DOWN_PORTS,
          "the plain `transfer` port still routes through the graph edge")
    check("default" not in STAND_DOWN_PORTS and "failed" not in STAND_DOWN_PORTS,
          "ordinary ports are unaffected")


def test_activation_validates_the_allowlist():
    from app.agents.service import validate_agent_config

    ok, _ = validate_agent_config({
        "engine": "dummy", "tools": {"transfer": True}, "transfer_targets": TARGETS,
    })
    check(not ok, "a well-formed allowlist activates cleanly")

    bad, _ = validate_agent_config({
        "engine": "dummy", "transfer_targets": {"x": {"kind": "number"}},
    })
    check(any("no target" in e for e in bad), "a target-less entry blocks activation")

    bad2, _ = validate_agent_config({
        "engine": "dummy", "transfer_targets": {"x": {"kind": "teleport", "target": "y"}},
    })
    check(any("unknown kind" in e for e in bad2), "an unknown kind blocks activation")

    _, warns = validate_agent_config({
        "engine": "dummy", "greeting": "hi", "persona": "p",
        "transfer_targets": TARGETS,   # but the transfer tool is OFF
    })
    check(any("toggled off" in w for w in warns),
          "declaring destinations with the transfer tool off is warned about")


def test_only_usable_names_are_offered():
    """A malformed entry is unreachable, so offering it would invite the model to pick
    something that silently cannot work."""
    check(target_names(TARGETS) == ["dispatch", "sales", "spanish"], "all valid names offered")
    mixed = {**TARGETS, "broken": {"kind": "number"}}
    check("broken" not in target_names(mixed), "a malformed entry is not offered to the model")
    check(target_names(None) == [], "no allowlist offers nothing")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    ran = 0
    for t in tests:
        if t.__name__ == "test_openai_schema_enumerates_only_declared_names":
            continue  # needs owen-voice on the path; covered there
        print(f"\n{t.__name__}")
        t()
        ran += 1
    print(f"\n{_checks} checks passed across {ran} tests.")

"""Unit test for the pure call-flow graph validator (app.flows.validator.validate_graph).

No API, no DB — a graph dict in, {errors, warnings} out. Confirms:
- a well-formed graph passes with no errors AND no warnings;
- each HARD ERROR case blocks activation (result.ok is False);
- each WARNING case is reported but still allows activation (result.ok is True).

Run: python -m tests.test_flow_validator
"""

import sys

from app.flows.validator import validate_graph


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"validator failed at: {name}")


def _valid_graph():
    """Fully wired, reachable, acyclic graph exercising every port kind — no warnings."""
    return {
        "default_fallback": "vm",
        "nodes": {
            "start": {"type": "entry", "next": {"default": "hours"}},
            "hours": {"type": "hours", "next": {"open": "greet", "closed": "vm"}},
            "greet": {"type": "play", "consent_notice": True, "next": {"default": "menu"}},
            "menu": {"type": "menu", "next": {"1": "sales", "2": "agent"}},
            "sales": {"type": "dial", "record": True, "next": {"answered": "bye", "noanswer": "vm", "busy": "vm", "failed": "vm"}},
            "agent": {"type": "ai_agent", "next": {"default": "bye", "transfer": "sales", "complete": "bye"}},
            "vm": {"type": "voicemail", "next": {"default": "bye"}},
            "bye": {"type": "hangup"},
        },
    }


def main():
    print("validate_graph — valid graph:")
    res = validate_graph(_valid_graph())
    check("valid graph has no errors", res.errors == [])
    check("valid graph has no warnings", res.warnings == [])
    check("valid graph is activatable (ok)", res.ok is True)

    print("validate_graph — HARD ERRORS block activation:")

    # No entry node.
    g = _valid_graph()
    g["nodes"]["start"]["type"] = "play"
    res = validate_graph(g)
    check("zero entry -> error", any("entry" in e for e in res.errors) and not res.ok)

    # Two entry nodes.
    g = _valid_graph()
    g["nodes"]["greet"]["type"] = "entry"
    res = validate_graph(g)
    check("two entries -> error", any("exactly one" in e for e in res.errors) and not res.ok)

    # Dangling edge target.
    g = _valid_graph()
    g["nodes"]["menu"]["next"]["1"] = "ghost"
    res = validate_graph(g)
    check("dangling target -> error", any("unknown node 'ghost'" in e for e in res.errors) and not res.ok)

    # Port not type-correct for the node type (hangup is terminal — no ports allowed).
    g = _valid_graph()
    g["nodes"]["bye"]["next"] = {"default": "vm"}
    res = validate_graph(g)
    check("invalid port on hangup -> error", any("invalid port" in e for e in res.errors) and not res.ok)

    # Port not type-correct: an hours node cannot have a DTMF digit port.
    g = _valid_graph()
    g["nodes"]["hours"]["next"]["5"] = "vm"
    res = validate_graph(g)
    check("digit port on hours -> error", any("invalid port '5'" in e for e in res.errors) and not res.ok)

    # Unresolvable default_fallback.
    g = _valid_graph()
    g["default_fallback"] = "nope"
    res = validate_graph(g)
    check("bad default_fallback -> error", any("default_fallback" in e for e in res.errors) and not res.ok)

    # Unknown node type.
    g = _valid_graph()
    g["nodes"]["agent"]["type"] = "frobnicate"
    res = validate_graph(g)
    check("unknown node type -> error", any("unknown type" in e for e in res.errors) and not res.ok)

    # Empty graph.
    res = validate_graph({"nodes": {}})
    check("empty graph -> error", not res.ok)

    print("validate_graph — WARNINGS reported but DO NOT block activation:")

    # Unreachable node (wired correctly, just not reachable from entry/fallback).
    g = _valid_graph()
    g["nodes"]["orphan"] = {"type": "play", "next": {"default": "bye"}}
    res = validate_graph(g)
    check("unreachable node -> warning", any("unreachable" in w for w in res.warnings))
    check("unreachable node still activatable", res.ok is True)

    # Unwired expected port (hours missing its 'closed' port).
    g = _valid_graph()
    del g["nodes"]["hours"]["next"]["closed"]
    res = validate_graph(g)
    check("unwired port -> warning", any("unwired port 'closed'" in w for w in res.warnings))
    check("unwired port still activatable", res.ok is True)

    # Cycle (greet -> menu already; make menu -> greet to form a cycle).
    g = _valid_graph()
    g["nodes"]["menu"]["next"]["2"] = "greet"
    res = validate_graph(g)
    check("cycle -> warning", any("cycle" in w for w in res.warnings))
    check("cycle still activatable", res.ok is True)

    print("\nALL FLOW VALIDATOR CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)

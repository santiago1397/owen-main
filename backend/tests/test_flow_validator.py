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
            "agent": {"type": "ai_agent", "next": {"default": "bye", "transfer": "sales", "complete": "bye", "failed": "vm"}},
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

    # Ticket 15.4: the ai_agent GRAPH port vocabulary is {default, transfer, complete,
    # failed}; the engine-side "end_call" is mapped to "complete" at the interpreter seam
    # and is NOT a valid graph port.
    g = _valid_graph()
    g["nodes"]["agent"]["next"]["end_call"] = "bye"
    res = validate_graph(g)
    check("'end_call' port on ai_agent -> error",
          any("invalid port 'end_call'" in e for e in res.errors) and not res.ok)

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

    # Ticket 15.4: every ai_agent port is EXPECTED — an unwired 'failed' warns, never blocks.
    g = _valid_graph()
    del g["nodes"]["agent"]["next"]["failed"]
    res = validate_graph(g)
    check("unwired ai_agent 'failed' -> warning", any("unwired port 'failed'" in w for w in res.warnings))
    check("unwired ai_agent 'failed' still activatable", res.ok is True)

    # Cycle (greet -> menu already; make menu -> greet to form a cycle).
    g = _valid_graph()
    g["nodes"]["menu"]["next"]["2"] = "greet"
    res = validate_graph(g)
    check("cycle -> warning", any("cycle" in w for w in res.warnings))
    check("cycle still activatable", res.ok is True)

    ticket17()

    print("\nALL FLOW VALIDATOR CHECKS PASSED")


def _parity_graph():
    """Ticket 17: a fully wired graph exercising the four parity node types — no warnings."""
    return {
        "default_fallback": "vm",
        "nodes": {
            "start": {"type": "entry", "next": {"default": "setv"}},
            "setv": {"type": "set_vars", "vars": {"greeting": "Hi {{caller_number}}"},
                     "next": {"default": "req"}},
            "req": {"type": "request", "method": "GET", "url": "https://api.example.com/x",
                    "next": {"success": "cond", "failure": "vm"}},
            "cond": {"type": "conditions",
                     "rows": [
                         {"variable": "request.body.status", "operator": "equals",
                          "value": "open", "port": "match_1"},
                         {"variable": "call.dow", "operator": "equals", "value": "sat",
                          "port": "match_2"},
                     ],
                     "next": {"match_1": "sms", "match_2": "vm", "else": "unsetv"}},
            "sms": {"type": "send_sms", "body": "Thanks for calling!", "next": {"default": "bye"}},
            "unsetv": {"type": "unset_vars", "names": ["greeting"], "next": {"default": "bye"}},
            "vm": {"type": "voicemail", "next": {"default": "bye"}},
            "bye": {"type": "hangup"},
        },
    }


def ticket17():
    print("validate_graph — Ticket 17 parity nodes:")
    res = validate_graph(_parity_graph())
    check("parity graph has no errors", res.errors == [])
    check("parity graph has no warnings", res.warnings == [])

    # set_vars/unset_vars/send_sms only allow `default`.
    g = _parity_graph()
    g["nodes"]["setv"]["next"]["success"] = "bye"
    res = validate_graph(g)
    check("non-default port on set_vars -> error",
          any("invalid port 'success'" in e for e in res.errors) and not res.ok)
    g = _parity_graph()
    g["nodes"]["sms"]["next"]["sent"] = "bye"
    res = validate_graph(g)
    check("invalid port on send_sms -> error",
          any("invalid port 'sent'" in e for e in res.errors) and not res.ok)

    # request only allows success/failure.
    g = _parity_graph()
    g["nodes"]["req"]["next"]["default"] = "bye"
    res = validate_graph(g)
    check("'default' port on request -> error",
          any("invalid port 'default'" in e for e in res.errors) and not res.ok)

    # conditions ports are DYNAMIC: each row's port + else. An unconfigured port errors.
    g = _parity_graph()
    g["nodes"]["cond"]["next"]["match_9"] = "bye"
    res = validate_graph(g)
    check("port with no matching conditions row -> error",
          any("invalid port 'match_9'" in e for e in res.errors) and not res.ok)

    # `else` is EXPECTED on conditions: leaving it unwired warns, never blocks.
    g = _parity_graph()
    del g["nodes"]["cond"]["next"]["else"]
    res = validate_graph(g)
    check("unwired conditions 'else' -> warning",
          any("unwired port 'else'" in w for w in res.warnings))
    check("unwired 'else' still activatable", res.ok is True)

    # Unwired row port also warns (falls through to default_fallback at runtime).
    g = _parity_graph()
    del g["nodes"]["cond"]["next"]["match_2"]
    res = validate_graph(g)
    check("unwired conditions row port -> warning",
          any("unwired port 'match_2'" in w for w in res.warnings))
    check("unwired row port still activatable", res.ok is True)

    # Unwired request `failure` warns (expected port).
    g = _parity_graph()
    del g["nodes"]["req"]["next"]["failure"]
    res = validate_graph(g)
    check("unwired request 'failure' -> warning",
          any("unwired port 'failure'" in w for w in res.warnings))
    check("unwired request 'failure' still activatable", res.ok is True)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)

"""Unit test for the pure flow-variable helpers (app.flows.variables) — Ticket 17.

Dependency-free (stdlib only, like test_flow_validator): covers {{var}} interpolation,
dot-path resolution (incl. request.body.* into parsed JSON and list indices), and every
`conditions` operator (first-match-wins, numeric vs string gt/lt, bad-regex row skip).

Run: python -m tests.test_flow_variables
"""

import sys

from app.flows.variables import evaluate_conditions, interpolate, resolve_var


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"flow_variables failed at: {name}")


VARS = {
    "caller_number": "+13055550123",
    "dialed_number": "+13055559999",
    "call.time": "14:05",
    "call.dow": "wed",
    "gather.digits": "12",
    "request.status": 200,
    "request.body": {"data": {"status": "open", "tags": ["a", "b"]}, "count": 3},
    "empty": "",
}


def test_interpolation():
    print("interpolate — {{var}} substitution:")
    check("simple var", interpolate("Hi {{caller_number}}!", VARS) == "Hi +13055550123!")
    check("whitespace inside braces", interpolate("{{ call.dow }}", VARS) == "wed")
    check("multiple vars",
          interpolate("{{call.dow}} {{call.time}}", VARS) == "wed 14:05")
    check("unknown var -> empty string", interpolate("x{{nope}}y", VARS) == "xy")
    check("unknown dotted var -> empty", interpolate("[{{request.body.data.missing}}]", VARS) == "[]")
    check("non-string value str()'d", interpolate("s={{request.status}}", VARS) == "s=200")
    check("None text -> empty string", interpolate(None, VARS) == "")
    check("text without templates untouched", interpolate("plain text", VARS) == "plain text")
    check("dotted built-in is a DIRECT key", interpolate("{{call.time}}", VARS) == "14:05")


def test_dot_path_resolution():
    print("resolve_var — direct keys + dot-paths into parsed JSON:")
    check("direct key", resolve_var(VARS, "caller_number") == "+13055550123")
    check("direct dotted key wins over path walk", resolve_var(VARS, "request.status") == 200)
    check("dot-path into request.body", resolve_var(VARS, "request.body.data.status") == "open")
    check("dot-path to a number", resolve_var(VARS, "request.body.count") == 3)
    check("dot-path list index", resolve_var(VARS, "request.body.data.tags.1") == "b")
    check("bad list index -> None", resolve_var(VARS, "request.body.data.tags.9") is None)
    check("path off a scalar -> None", resolve_var(VARS, "request.body.count.x") is None)
    check("unknown root -> None", resolve_var(VARS, "nope.deep.path") is None)
    check("empty name -> None", resolve_var(VARS, "") is None)


def _row(variable, operator, value, port="p"):
    return {"variable": variable, "operator": operator, "value": value, "port": port}


def test_condition_operators():
    print("evaluate_conditions — each operator:")
    def match(row, variables=VARS):
        idx, port, _ = evaluate_conditions([row], variables)
        return port is not None

    check("equals hit", match(_row("call.dow", "equals", "wed")))
    check("equals miss", not match(_row("call.dow", "equals", "thu")))
    check("not_equals hit", match(_row("call.dow", "not_equals", "thu")))
    check("contains hit", match(_row("caller_number", "contains", "305")))
    check("contains miss", not match(_row("caller_number", "contains", "786")))
    check("regex hit", match(_row("caller_number", "regex", r"^\+1305")))
    check("regex miss", not match(_row("caller_number", "regex", r"^\+1786")))
    check("gt numeric (12 > 5 despite '12'<'5' lexically)", match(_row("gather.digits", "gt", "5")))
    check("lt numeric miss", not match(_row("gather.digits", "lt", "5")))
    check("gt string compare when non-numeric", match(_row("call.dow", "gt", "tue")))
    check("lt string compare when non-numeric", match(_row("call.dow", "lt", "wee")))
    check("is_empty hit on empty string", match(_row("empty", "is_empty", "")))
    check("is_empty hit on missing var", match(_row("no_such_var", "is_empty", "")))
    check("is_empty miss", not match(_row("call.dow", "is_empty", "")))
    check("missing var compares as empty string", match(_row("no_such_var", "equals", "")))
    check("value interpolates {{var}}", match(_row("caller_number", "equals", "{{caller_number}}")))


def test_first_match_wins_and_row_skipping():
    print("evaluate_conditions — ordering, skips, else:")
    rows = [
        _row("call.dow", "equals", "thu", port="r1"),          # miss
        _row("call.dow", "regex", "([bad", port="r2"),          # bad regex -> skipped
        {"variable": "call.dow"},                                # malformed -> skipped
        "not-a-dict",                                            # malformed -> skipped
        _row("call.dow", "equals", "wed", port="r3"),           # first real match
        _row("caller_number", "contains", "305", port="r4"),    # would match, but later
    ]
    idx, port, actual = evaluate_conditions(rows, VARS)
    check("first matching row wins (index 4)", idx == 4)
    check("matched row's port returned", port == "r3")
    check("actual value returned", actual == "wed")

    idx, port, actual = evaluate_conditions([_row("call.dow", "equals", "thu")], VARS)
    check("no match -> (None, None, None)", idx is None and port is None and actual is None)
    check("rows not a list -> no match", evaluate_conditions("nope", VARS) == (None, None, None))
    check("unknown operator row skipped",
          evaluate_conditions([_row("call.dow", "frobnicate", "wed")], VARS) == (None, None, None))
    check("row without port skipped",
          evaluate_conditions([_row("call.dow", "equals", "wed", port=None)], VARS) == (None, None, None))


def main():
    test_interpolation()
    test_dot_path_resolution()
    test_condition_operators()
    test_first_match_wins_and_row_skipping()
    print("\nALL FLOW VARIABLES CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)

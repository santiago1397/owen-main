"""Pure tests for `${ENV_VAR}` resolution in custom-tool headers (VOICE_STACK_MIGRATION M14).

The property under test is a SECURITY one, so it is tested rather than assumed: a tool
declaration lives in `agent_versions.config` — immutable, versioned forever, readable through
the agents API — and a CRM key written there would be plaintext in the database, copied into
every later version row, and rotatable only by re-authoring every agent that used it.

Run:  python -m tests.test_custom_tool_headers      (from owen-voice/)
"""

import os
import sys

sys.path.insert(0, ".")

from app.custom_tools import normalise, resolve_headers  # noqa: E402

_checks = 0
_failures = []


def check(cond: bool, label: str) -> None:
    global _checks
    _checks += 1
    if not cond:
        _failures.append(label)
        print(f"  FAIL  {label}")
    else:
        print(f"  ok    {label}")


def test_env_reference_is_expanded():
    print("\ntest_env_reference_is_expanded")
    os.environ["TEST_CRM_KEY"] = "s3cr3t"
    out = resolve_headers({"Authorization": "Bearer ${TEST_CRM_KEY}"})
    check(out["Authorization"] == "Bearer s3cr3t", "the variable is substituted")
    check("${" not in out["Authorization"], "no placeholder survives onto the wire")


def test_the_secret_is_not_in_the_declaration():
    print("\ntest_the_secret_is_not_in_the_declaration")
    os.environ["TEST_CRM_KEY"] = "s3cr3t"
    declared = [{"name": "lookup", "url": "https://crm.example/x",
                 "headers": {"Authorization": "Bearer ${TEST_CRM_KEY}"}}]
    stored = normalise(declared)
    # This is the whole point: what gets PERSISTED carries a variable name, not a credential.
    check("s3cr3t" not in str(stored), "the stored declaration contains no secret")
    check("${TEST_CRM_KEY}" in str(stored), "it carries the reference instead")
    check(resolve_headers(stored[0]["headers"])["Authorization"] == "Bearer s3cr3t",
          "and it still resolves at call time")


def test_unset_variable_expands_to_empty_not_the_placeholder():
    print("\ntest_unset_variable_expands_to_empty_not_the_placeholder")
    os.environ.pop("TEST_ABSENT_KEY", None)
    out = resolve_headers({"Authorization": "Bearer ${TEST_ABSENT_KEY}"})
    check(out["Authorization"] == "Bearer ", "an unset var becomes empty")
    # Leaking the literal `${TEST_ABSENT_KEY}` would fail the request anyway AND write the
    # variable's name into somebody else's access log.
    check("TEST_ABSENT_KEY" not in out["Authorization"],
          "the variable NAME does not travel to the third party")


def test_plain_headers_are_untouched():
    print("\ntest_plain_headers_are_untouched")
    out = resolve_headers({"Accept": "application/json", "X-Tenant": "owen"})
    check(out == {"Accept": "application/json", "X-Tenant": "owen"},
          "headers with no reference pass through verbatim")
    check(resolve_headers(None) == {}, "absent headers are an empty dict, never a crash")


def test_multiple_and_repeated_references():
    print("\ntest_multiple_and_repeated_references")
    os.environ["TEST_A"] = "aa"
    os.environ["TEST_B"] = "bb"
    out = resolve_headers({"X": "${TEST_A}-${TEST_B}-${TEST_A}"})
    check(out["X"] == "aa-bb-aa", "every occurrence is replaced, not just the first")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_checks} checks, {len(_failures)} failed")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1 if _failures else 0)

"""Caller-context kernel (CRM_CONTEXT_SPEC C3/C4/C7/C9). Pure — no DB, no CRM.

Two rules carry real consequences and both are tested here:

  C3  when we are NOT sure enough to use a name. Greeting the wrong person by name is far
      worse than not greeting them at all.
  C4  what may be injected. Everything rendered here reaches the model AND a `transcriptions`
      row that `api/ai/content.py` serves to any key holding the `content` scope.

Run:  python -m tests.test_caller_context      (from backend/)
"""

import sys

sys.path.insert(0, ".")

from app.agents.context import (  # noqa: E402
    filter_facts,
    merge_identity,
    normalise_phone,
    render_blob,
    same_caller,
    validate_provider,
)

_checks = 0


def check(cond, label):
    global _checks
    _checks += 1
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    print(f"  ok  {label}")


# --- C3: identity -------------------------------------------------------------------------

def test_normalisation_collapses_real_variation():
    for form in ["+19549147244", "19549147244", "9549147244", "(954) 914-7244", "954-914-7244"]:
        check(normalise_phone(form) == "9549147244", f"{form} normalises")


def test_matching_is_not_fuzzy():
    check(same_caller("+19549147244", "954 914 7244"), "the same number in two formats matches")
    check(not same_caller("+19549147244", "+19549147245"), "one digit different does NOT match")
    check(not same_caller("911", "911"), "a short number never matches — not enough to be sure")
    check(not same_caller("", ""), "empty never matches")


# --- C4: the allowlist --------------------------------------------------------------------

FACTS = {"email": "a@b.com", "balance": "1200", "ssn": "000-00-0000", "address": "12 Oak St"}


def test_only_declared_fields_survive():
    out = filter_facts(FACTS, ["email", "address"])
    check(set(out) == {"email", "address"}, "declared keys pass")
    check("ssn" not in out and "balance" not in out, "undeclared keys are dropped")


def test_no_allowlist_means_nothing_not_everything():
    """The failure mode of the other default is a caller's SSN in a stored transcript."""
    check(filter_facts(FACTS, []) == {}, "an empty allowlist yields nothing")
    check(filter_facts(FACTS, None) == {}, "a missing allowlist yields nothing")


def test_values_are_capped():
    out = filter_facts({"notes": "x" * 5000}, ["notes"])
    check(len(out["notes"]) <= 200, "a huge value is truncated, not re-billed every turn")


# --- C9: who owns identity ------------------------------------------------------------------

def test_owen_wins_identity_the_crm_does_not():
    name, src = merge_identity({"display_name": "Bob"}, {"display_name": "Robert Nguyen"})
    check(name == "Bob" and src == "owen",
          "a human-entered label beats the CRM — humans win over models")
    name, src = merge_identity({}, {"display_name": "Robert Nguyen"})
    check(name == "Robert Nguyen" and src == "provider", "the CRM fills in when OWEN cannot")
    name, src = merge_identity({}, {})
    check(name is None and src == "unknown", "neither knows -> no name is invented")


# --- C7: the rendered blob -------------------------------------------------------------------

def test_blob_is_empty_when_nothing_is_known():
    """An agent told 'the caller is unknown' tends to announce it."""
    blob, fields = render_blob({}, {}, ["email"])
    check(blob == "" and fields == [], "nothing known -> nothing injected")


def test_blob_carries_name_history_summary_and_facts():
    blob, fields = render_blob(
        {"display_name": "Maria", "history": "This caller has called 4 times before."},
        {"summary": "3 open invoices.", "facts": FACTS},
        ["email", "address"],
    )
    check("Maria" in blob, "the name is there")
    check("4 times" in blob, "OWEN's own history is there — no CRM needed for this")
    check("3 open invoices" in blob, "the CRM summary is there")
    check("a@b.com" in blob, "an allowlisted fact is there")
    check("000-00-0000" not in blob, "an UNdeclared fact never reaches the model")
    check("do not read it back" in blob.lower(),
          "the model is told not to recite it — otherwise it reads like surveillance")


def test_injected_names_are_returned_for_logging_but_not_values():
    _blob, fields = render_blob(
        {"display_name": "Maria"}, {"facts": {"email": "a@b.com"}}, ["email"],
    )
    check("display_name" in fields and "email" in fields, "field NAMES are reported")
    check(not any("@" in f for f in fields), "no VALUE appears in what gets logged")


# --- C14/C16: provider config ------------------------------------------------------------------

def test_provider_validation():
    check(validate_provider(None) == [], "no provider configured is fine")
    check(validate_provider({"kind": "none"}) == [], "kind none is fine")
    check(any("unknown" in e for e in validate_provider({"kind": "salesforce"})),
          "an unknown kind blocks activation")
    check(any("needs a url" in e for e in validate_provider({"kind": "http", "allowlist": ["a"]})),
          "kind http with no url blocks activation")
    check(validate_provider({"kind": "http", "url": "https://x/lookup", "allowlist": ["a"]}) == [],
          "a well-formed http provider validates")
    check(any("no allowlist" in e for e in validate_provider({"kind": "ghl"})),
          "a provider with no allowlist is called out rather than silently useless")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"\n{t.__name__}")
        t()
    print(f"\n{_checks} checks passed across {len(tests)} tests.")

"""Derived AI-cost kernel (AI_AGENT_SPEC D14). Pure — no DB, no vendors.

The property under test is honesty, not arithmetic. `services/billing.py` was rewritten once
because an earlier design estimated usage cost and was wrong in both directions; the rule it
settled on is that a bill which quietly under-reports is worse than one that admits ignorance.
So: missing usage must surface as UNRATED, never as zero.

Run:  python -m tests.test_ai_cost      (from backend/)
"""

import sys
from decimal import Decimal

sys.path.insert(0, ".")

from app.services.ai_cost import (  # noqa: E402
    AI_KINDS,
    PROVENANCE_DERIVED,
    PROVENANCE_RATED,
    charges_for_session,
    llm_charge,
    session_total,
    stt_charge,
    tts_charge,
)

_checks = 0


def check(cond, label):
    global _checks
    _checks += 1
    if not cond:
        raise AssertionError(f"FAILED: {label}")
    print(f"  ok  {label}")


FULL = {
    "stt_model": "gpt-4o-mini-transcribe", "stt_audio_seconds": 45,
    "llm_model": "gpt-4o-mini", "llm_tokens_in": 2400, "llm_tokens_out": 320,
    "tts_model": "gpt-4o-mini-tts", "tts_characters": 900,
}


def test_a_complete_session_prices_every_stage():
    charges = charges_for_session(FULL)
    check(len(charges) == 3, "one line per stage")
    check({c.kind for c in charges} == set(AI_KINDS), "stt, llm and tts are all present")
    check(all(not c.unrated for c in charges), "nothing is unrated when usage is complete")
    total, any_unrated = session_total(charges)
    check(total > 0, f"a real total is produced ({total})")
    check(not any_unrated, "the total is complete")


def test_missing_usage_is_unrated_not_zero():
    """The rule the carrier kernel already holds itself to."""
    c = tts_charge("gpt-4o-mini-tts", None)
    check(c.unrated, "absent usage marks the line unrated")
    check(c.amount == Decimal("0"), "an unrated line carries no amount")
    check("usage" in (c.unrated_reason or ""), f"the reason says why ({c.unrated_reason})")

    partial = charges_for_session({"llm_model": "gpt-4o-mini",
                                   "llm_tokens_in": 100, "llm_tokens_out": 50})
    _total, any_unrated = session_total(partial)
    check(any_unrated,
          "a session missing a stage reports INCOMPLETE rather than a confident small number")


def test_unknown_model_is_unrated_not_guessed():
    c = llm_charge("some-new-model", 1000, 1000)
    check(c.unrated, "an unpriced model is unrated")
    check("rate" in (c.unrated_reason or ""), "the reason names the missing rate")
    check(c.amount == Decimal("0"), "no amount is invented for it")


def test_arithmetic():
    # 45s at $0.0001/s
    check(stt_charge("whisper-1", 45).amount == Decimal("0.004500"), "stt: seconds x rate")
    # 900 chars at $0.015/1k
    check(tts_charge("tts-1", 900).amount == Decimal("0.013500"), "tts: chars/1000 x rate")
    # 2400 in @0.00015/1k + 320 out @0.0006/1k
    check(llm_charge("gpt-4o-mini", 2400, 320).amount == Decimal("0.000552"),
          "llm: in and out priced separately")


def test_llm_splits_input_and_output():
    """Output tokens cost several times input; a blended rate would misprice every call."""
    a = llm_charge("gpt-4o-mini", 1000, 0).amount
    b = llm_charge("gpt-4o-mini", 0, 1000).amount
    check(b > a, "output tokens are priced higher than input")


def test_provenance_values_are_distinct():
    check(PROVENANCE_DERIVED != PROVENANCE_RATED,
          "derived and rated are different values, not a convention")
    check(PROVENANCE_DERIVED == "derived" and PROVENANCE_RATED == "rated",
          "and they are the values the migration defaults to")


def test_usage_is_preserved_verbatim():
    """So a rate correction can be re-applied later without re-running any calls."""
    c = llm_charge("gpt-4o-mini", 2400, 320)
    check(c.usage == {"tokens_in": 2400, "tokens_out": 320}, "reported usage is kept")
    check(stt_charge("whisper-1", 45).usage["audio_seconds"] == 45.0, "stt usage is kept")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"\n{t.__name__}")
        t()
    print(f"\n{_checks} checks passed across {len(tests)} tests.")

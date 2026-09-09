"""The agent-config guardrails added by VOICE_STACK_MIGRATION (M4/M5/M11).

Two rules, both of which exist because the failure they prevent is SILENT:

  M11  `knowledge` is concatenated whole into the system prompt by a property recomputed on
       EVERY turn. Until the cap it was the one completely unbounded input in the loop — no
       truncation in the UI, the schema, the spec, the wire or the prompt — so a large blob
       was re-billed per turn with nothing anywhere saying so.
  M5   Changing TTS vendor invalidates every stored voice string at once. That must WARN,
       never block: refusing to activate agents that were fine yesterday is worse than one
       call in the default voice.

Run:  python -m tests.test_agent_config_limits      (from backend/)
"""

import sys

sys.path.insert(0, ".")

from app.agents.service import (  # noqa: E402
    KNOWLEDGE_MAX_CHARS,
    build_spec,
    validate_agent_config,
    voice_warning,
)

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


BASE = {"engine": "owen_voice", "persona": "p", "greeting": "g"}


def test_knowledge_within_budget_activates():
    print("\ntest_knowledge_within_budget_activates")
    errors, _ = validate_agent_config({**BASE, "knowledge": "x" * KNOWLEDGE_MAX_CHARS})
    check(errors == [], "exactly at the limit is allowed")


def test_oversized_knowledge_blocks_activation():
    print("\ntest_oversized_knowledge_blocks_activation")
    errors, _ = validate_agent_config({**BASE, "knowledge": "x" * (KNOWLEDGE_MAX_CHARS + 1)})
    check(len(errors) == 1, "one character over is refused")
    check("every turn" in errors[0].lower() or "turn" in errors[0].lower(),
          f"the message explains WHY it is capped ({errors[0][:60]}...)")
    check(str(KNOWLEDGE_MAX_CHARS) in errors[0], "and names the limit")


def test_legacy_versions_are_truncated_not_trusted():
    print("\ntest_legacy_versions_are_truncated_not_trusted")
    # Versions activated BEFORE the cap existed are immutable and still runnable. Without this
    # they would re-bill an unbounded prompt on every turn, forever.
    spec = build_spec("a1", "v1", {"knowledge": "y" * (KNOWLEDGE_MAX_CHARS * 3)})
    check(len(spec.knowledge) == KNOWLEDGE_MAX_CHARS,
          "an over-budget legacy version is truncated for the prompt")
    short = build_spec("a1", "v1", {"knowledge": "y" * 10})
    check(short.knowledge == "y" * 10, "anything within budget is untouched")


def test_wrong_provider_voice_warns_but_never_blocks():
    print("\ntest_wrong_provider_voice_warns_but_never_blocks")
    errors, warnings = validate_agent_config(
        {**BASE, "tts_provider": "deepgram", "voice": "alloy"})
    check(errors == [], "a stale voice does NOT block activation")
    check(any("alloy" in w for w in warnings), "but it is warned about")
    check(any("default" in w for w in warnings), "and says what will happen instead")


def test_matching_voices_are_silent():
    print("\ntest_matching_voices_are_silent")
    check(voice_warning("deepgram", "aura-2-thalia-en") is None, "a real Aura-2 voice is fine")
    check(voice_warning("openai", "alloy") is None, "a real OpenAI voice is fine")
    check(voice_warning("deepgram", "") is None, "blank means server default, not an error")
    check(voice_warning("", "alloy") is None, "no provider set falls back to the OpenAI list")


def test_deepgram_voices_are_matched_by_family_not_enumeration():
    print("\ntest_deepgram_voices_are_matched_by_family_not_enumeration")
    # Deepgram ships ~49 English voices and adds more; an enumeration would be stale within a
    # month and would start warning about voices that are perfectly valid.
    check(voice_warning("deepgram", "aura-2-someone-new-en") is None,
          "an unlisted but well-formed Aura-2 voice does not warn")
    check(voice_warning("deepgram", "nova") is not None,
          "an OpenAI voice under Deepgram still warns")


def test_provider_pins_are_optional():
    print("\ntest_provider_pins_are_optional")
    errors, _ = validate_agent_config(BASE)
    check(errors == [], "no pin at all is valid — the env default decides (M4)")
    spec = build_spec("a1", "v1", {**BASE, "stt_provider": "deepgram"})
    check(spec.config.get("stt_provider") == "deepgram",
          "a pin survives into the spec config for the wire")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{_checks} checks, {len(_failures)} failed")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1 if _failures else 0)

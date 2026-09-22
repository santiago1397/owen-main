"""What the CRM is told about a call the AI agent answered.

Phase 1 of the CRM's voice-agent amendment (ghl-clone DECISIONS.md, 2026-09-22). The
agent has been answering real calls since 2026-09-09 and the CRM has never shown one: no
agent name, no transcript, no capture. This is the payload that closes that, checked
against the contract `validate_crm_event` records — so a drift fails here rather than as a
422 inside a five-attempt retry loop against a live CRM.

Two rules are worth more than the rest:

  * **The CALL's outcome is `answered`, whatever port the AGENT exited on.** The agent
    picking up IS the call being answered; reporting `failed` because the conversation
    ended awkwardly would file a real conversation as a missed call and, with the CRM's
    missed-call rule re-armed, text the customer about it.
  * **Every report carries a dedupe_key.** The worker retries a delivery five times, and a
    POST that timed out AFTER the CRM inserted the row is indistinguishable from one that
    never arrived. Without the key, the customer's thread grows a second call.

Run:  python -m tests.test_agent_crm_report      (from backend/)
"""

import sys

sys.path.insert(0, ".")

from app.agents.crm_call import (  # noqa: E402
    ai_call_record,
    dedupe_key,
    report_extra,
    transcript_text,
)
from app.integrations.crm.events import (  # noqa: E402
    CallEventFacts,
    to_crm_event,
    validate_crm_event,
)

_checks = 0
_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    global _checks
    _checks += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        _failures.append(label)


TURNS = [
    {"speaker": "caller", "text": "my roof is leaking in the kitchen"},
    {"speaker": "agent", "text": "I'm sorry about that. Can I get your name?"},
    {"speaker": "caller", "text": "Maria Ruiz, 412 Palm Ave"},
]

DATA = {
    "captured": {"name": "Maria Ruiz", "address": "412 Palm Ave",
                 "intent": "active leak in the kitchen", "urgency": "emergency"},
    "transcript": TURNS,
    "duration_s": 96.4,
}


def facts(port: str = "end_call", data: dict | None = None) -> CallEventFacts:
    extra = report_extra(agent_name="Roofing Receptionist", version=10, outcome=port,
                         data=DATA if data is None else data, campaign="Quo overflow",
                         owen_call_id="7781")
    return CallEventFacts(
        phase="ended", owen_call_id="7781", linkedid="1790109240.5",
        caller_number="+18135550142", dialed_number="+19546859990",
        direction="inbound", outcome="answered", duration_seconds=96, extra=extra)


def test_the_body_carries_the_agent_the_capture_and_the_words():
    body = to_crm_event(facts())
    check("the CRM will accept it", validate_crm_event(body) == [])
    check("it is a CALL", body["type"] == "CALL")
    check("the agent is named", body["ai_call"]["agent"] == "Roofing Receptionist")
    check("the version rides along, so a bad answer traces to a prompt",
          body["ai_call"]["version"] == 10)
    check("the campaign is carried for attribution",
          body["ai_call"]["campaign"] == "Quo overflow")
    check("what the caller gave is captured",
          body["ai_call"]["captured"]["name"] == "Maria Ruiz"
          and body["ai_call"]["captured"]["address"] == "412 Palm Ave")
    check("an emergency is marked as one",
          body["ai_call"]["captured"]["urgency"] == "emergency")
    check("the words are sent, speaker-labelled",
          body["transcript"].startswith("caller: my roof is leaking"))
    check("the caller's number is always sent, so a stranger still lands somewhere",
          body["from_number"] == "+18135550142")
    check("the join key back to OWEN is the call id",
          body["provider_ref"] == "7781")


def test_the_call_was_answered_whatever_the_agent_did():
    for port in ("end_call", "transfer", "default", "failed"):
        body = to_crm_event(facts(port))
        check(f"a call the agent exited on {port!r} is reported as completed",
              body["call_status"] == "completed")
    # ...and the agent's own outcome is still recorded, where it belongs.
    check("the agent's outcome is kept separately",
          to_crm_event(facts("transfer"))["ai_call"]["outcome"] == "transfer")
    check("the graph's `complete` port is translated to the engine's word",
          ai_call_record(agent_name="a", version=1, outcome="complete",
                         captured=None)["outcome"] == "end_call")


def test_every_report_is_idempotent():
    body = to_crm_event(facts())
    check("a dedupe_key is present", bool(body.get("dedupe_key")))
    check("it is keyed on the CALL, not the attempt",
          body["dedupe_key"] == dedupe_key("7781"))
    check("two reports of the same call carry the same key",
          to_crm_event(facts())["dedupe_key"] == to_crm_event(facts("transfer"))["dedupe_key"])
    check("it fits the CRM's column", len(body["dedupe_key"]) <= 200)


def test_a_call_no_agent_touched_says_nothing_about_one():
    empty = CallEventFacts(phase="ended", owen_call_id="9", linkedid="1.1",
                           caller_number="+18135550142", dialed_number="+19546859990",
                           outcome="answered")
    body = to_crm_event(empty)
    check("no ai_call key at all", "ai_call" not in body)
    check("no transcript key at all", "transcript" not in body)
    check("...and it is still a valid event", validate_crm_event(body) == [])
    check("report_extra returns nothing when the agent captured nothing and said nothing",
          report_extra(agent_name="", version=None, outcome="", data={}) == {})


def test_an_empty_capture_is_omitted_rather_than_sent_hollow():
    # The CRM MERGES these records across the two reports of one call. An empty dict would
    # occupy the key and block the real capture when it arrives at the end of the call.
    record = ai_call_record(agent_name="Roofing Receptionist", version=10,
                            outcome="end_call", captured={})
    check("no captured key when nothing was captured", "captured" not in record)
    record = ai_call_record(agent_name="Roofing Receptionist", version=10,
                            outcome="end_call", captured={"name": "", "intent": None})
    check("blank values do not count as a capture", "captured" not in record)


def test_the_transcript_reads_the_same_here_as_in_owen():
    text = transcript_text(TURNS)
    check("one line per turn", text.count("\n") == 2)
    check("speakers are labelled", text.startswith("caller: ") and "agent: " in text)
    check("junk is ignored rather than crashed on", transcript_text("not a list") == "")
    check("no turns is an empty string, not a fabricated line", transcript_text([]) == "")


def test_a_malformed_report_is_refused_here_not_by_the_crm():
    bad = CallEventFacts(phase="ended", owen_call_id="7", linkedid="1.1",
                         caller_number="+18135550142", dialed_number="+19546859990",
                         outcome="answered",
                         extra={"ai_call": "not an object", "dedupe_key": "k" * 201})
    problems = validate_crm_event(to_crm_event(bad))
    check("an ai_call that is not an object is caught",
          any("ai_call" in p for p in problems))
    check("a dedupe_key longer than the CRM's column is caught — a truncated key would "
          "collide with a DIFFERENT call",
          any("dedupe_key" in p for p in problems))


if __name__ == "__main__":
    test_the_body_carries_the_agent_the_capture_and_the_words()
    test_the_call_was_answered_whatever_the_agent_did()
    test_every_report_is_idempotent()
    test_a_call_no_agent_touched_says_nothing_about_one()
    test_an_empty_capture_is_omitted_rather_than_sent_hollow()
    test_the_transcript_reads_the_same_here_as_in_owen()
    test_a_malformed_report_is_refused_here_not_by_the_crm()
    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    for f in _failures:
        print(f"  FAILED: {f}")
    raise SystemExit(1 if _failures else 0)

"""The call and SMS ingest paths must resolve a DID to the SAME `numbers` row.

WHY. The rule was written twice and the copies drifted. `ingest_message_event` matched on
provider_id alone; six days later the CALL path was fixed for BulkVS split identity and the
SMS sibling was not revisited. Seven weeks later, 3 of 6 messages had no number and no
campaign — including one to a DID that has a campaign, so Campaign ROI was undercounting.

These checks pin two things:
  1. the rule itself (app/services/number_match.py) behaves for every provider shape;
  2. both ingest paths actually CALL it — so a third copy cannot quietly appear, which is
     the failure mode that cost the seven weeks.

Pure: compiles the SQL clause and reads source. No DB, no HTTP.

Run: python -m tests.test_number_match
"""

import pathlib
import sys

from app.services.number_match import owned_number_clause


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"number_match failed at: {name}")


def _sql(provider_id: int, provider_name: str) -> str:
    """The rendered WHERE fragment, with literals inlined so it can be asserted on."""
    return str(
        owned_number_clause(provider_id, provider_name).compile(
            compile_kwargs={"literal_binds": True}
        )
    )


def test_rule():
    print("the rule")
    sql = _sql(9, "bulkvs")
    check("matches on provider_id", "numbers.provider_id = 9" in sql)
    check("matches on owner_provider", "numbers.owner_provider = 'bulkvs'" in sql)
    check("matches on media_provider", "numbers.media_provider = 'bulkvs'" in sql)
    check("the three are OR'd, never AND'd", " OR " in sql and " AND " not in sql)

    # The real-world shapes, spelled out. A DID adopted from a legacy Twilio row keeps
    # provider_id=twilio while owner_provider='bulkvs' — the exact row that was being missed.
    call_sql = _sql(10, "asterisk")     # ARI consumer ingests calls as 'asterisk'
    sms_sql = _sql(9, "bulkvs")         # the MO webhook ingests SMS as 'bulkvs'
    check("a call reaches an adopted DID via media_provider",
          "numbers.media_provider = 'asterisk'" in call_sql)
    check("an SMS reaches the same DID via owner_provider",
          "numbers.owner_provider = 'bulkvs'" in sms_sql)

    # Legacy Twilio/SignalWire rows have owner/media NULL and must still match. NULL = 'x' is
    # never true in SQL, so those clauses simply do not fire and provider_id carries it.
    legacy = _sql(3, "twilio")
    check("legacy rows still matched by provider_id", "numbers.provider_id = 3" in legacy)

    # Widening only: whatever a provider_id-only lookup found, this still finds.
    check("provider_id is always one of the disjuncts",
          all(f"numbers.provider_id = {pid}" in _sql(pid, n)
              for pid, n in ((1, "signalwire"), (3, "twilio"), (9, "bulkvs"), (10, "asterisk"))))


def test_both_paths_use_it():
    """The point of the file: one rule, called from both places.

    Asserted on source rather than behaviour because the failure being prevented is someone
    hand-rolling the clause again — which a behavioural test on the shared function cannot
    see."""
    print("\nboth ingest paths use the shared rule")
    root = pathlib.Path(__file__).resolve().parents[1] / "app" / "services"
    ingestion = (root / "ingestion.py").read_text(encoding="utf-8")
    messages = (root / "messages.py").read_text(encoding="utf-8")

    for name, src in (("call path (ingestion.py)", ingestion), ("SMS path (messages.py)", messages)):
        check(f"{name} imports the rule", "from app.services.number_match import" in src)
        check(f"{name} calls owned_number_clause", "owned_number_clause(" in src)
        # A hand-rolled copy would look like this. The outbound branch of ingestion.py
        # deliberately narrows to BULKVS_MEDIA_PROVIDER and is not this shape.
        check(f"{name} has no hand-rolled provider_id/phone_number pair",
              "Number.provider_id == provider.id, Number.phone_number" not in src)


def main():
    test_rule()
    test_both_paths_use_it()
    print("\nALL NUMBER-MATCH CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)

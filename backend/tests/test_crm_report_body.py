"""What OWEN tells the CRM about a call its AI agent handled.

Two fields were wrong from the day this path was written, and neither could raise:

  1. **`transfer` was always null.** `flows/runtime.py` queued the report BEFORE resolving
     the agent's chosen destination, and the destination is only written into the result
     dict when the caller has actually been moved. So every transferred call reported
     "ended on port transfer" and refused to say where to. For an agent in its supervised
     phase — where "who did it hand the customer to?" is the question being reviewed —
     that is the one field that mattered.
  2. **`duration_s` was hardcoded `None`.** owen-voice measured it all along
     (`MediaSession.duration_s`) and never sent it; now it rides back on the session
     result.

Both live in `app.agents.report.build_report_body`, which is importable with nothing but
the stdlib, so this test runs in the sandbox where `flows/runtime.py` (httpx, sqlalchemy)
cannot even be imported. The ORDER fix in runtime.py cannot be executed here; what this
file pins is that the builder reports a destination WHEN IT IS GIVEN ONE, so a caller that
reverts to reporting too early fails on the `transferred` case below.

Run:  python -m tests.test_crm_report_body      (from backend/)
"""

import sys

sys.path.insert(0, ".")

from app.agents.report import KNOWN_OUTCOMES, build_report_body  # noqa: E402

_checks = 0
_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    global _checks
    _checks += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        _failures.append(label)


# The destination shape `resolve_transfer_target` returns and `runtime.py` merges into the
# result as `data["transfer"]` once the caller has been moved.
OFFICE = {"name": "office", "kind": "number", "target": "+19415550113"}


def test_a_transferred_call_says_where_it_went():
    body = build_report_body(
        linkedid="1758000000.1", caller_number="+18135550102", port="transferred",
        data={"transfer": OFFICE, "captured": {"name": "Maria"}, "duration_s": 74.5},
    )
    check("the outcome is the FINAL port, not the raw one", body["outcome"] == "transferred")
    check("the destination's NAME is reported", body["transfer"] == "office")
    check("...and not the whole target (a phone number is not the CRM's business)",
          body["transfer"] == "office" and "target" not in str(body["transfer"]))
    check("the duration survives", body["duration_s"] == 74.5)


def test_the_bug_itself_an_unresolved_transfer_reports_no_destination():
    # This is what the old ordering produced: port `transfer`, and a result dict that does
    # not yet carry the destination. It must be reported honestly as "no destination",
    # never invented -- the flow's own transfer edge took it from here.
    body = build_report_body(
        linkedid="1758000000.2", caller_number="+18135550102", port="transfer", data={},
    )
    check("an unresolved transfer reports transfer=None", body["transfer"] is None)
    check("and still reports the port it ended on", body["outcome"] == "transfer")


def test_duration_is_unknown_rather_than_zero():
    body = build_report_body(linkedid="x", caller_number="", port="failed", data={})
    check("a call with no duration reports None, not 0",
          body["duration_s"] is None)
    body = build_report_body(linkedid="x", caller_number="", port="failed",
                             data={"duration_s": 0})
    check("...and a reported 0 stays 0 (an instant failure is a real answer)",
          body["duration_s"] == 0.0)
    body = build_report_body(linkedid="x", caller_number="", port="end_call",
                             data={"metrics": {"duration_s": 31.2}})
    check("a duration hiding in the metrics blob is still found",
          body["duration_s"] == 31.2)
    body = build_report_body(linkedid="x", caller_number="", port="end_call",
                             data={"duration_s": "not a number"})
    check("rubbish is dropped rather than shipped", body["duration_s"] is None)


def test_captures_are_normalised_and_absent_when_empty():
    body = build_report_body(
        linkedid="x", caller_number="+18135550102", port="end_call",
        data={"captured": {"name": "Maria Ruiz", "address": "", "intent": "leak"}},
    )
    check("a capture is carried as one {'fields': ...} entry", len(body["captures"]) == 1)
    fields = body["captures"][0]["fields"]
    check("empty values are dropped by normalise_capture",
          "address" not in str(fields) or fields.get("address") not in ("", None))
    check("what the agent DID learn survives", "Maria Ruiz" in str(fields))

    body = build_report_body(linkedid="x", caller_number="", port="end_call",
                             data={"captured": {}})
    check("an empty capture produces no entry at all", body["captures"] == [])
    body = build_report_body(linkedid="x", caller_number="", port="end_call",
                             data={"captured": "not a dict"})
    check("a malformed capture is ignored, not crashed on", body["captures"] == [])


def test_the_body_is_exactly_the_contract():
    body = build_report_body(linkedid="1758.9", caller_number="+18135550102",
                             port="end_call", data={}, owen_url="https://owen/c/1758.9")
    check("the keys are the ones the CRM report handler reads",
          set(body) == {"linkedid", "caller_number", "outcome", "duration_s",
                        "captures", "transfer", "owen_url"})
    check("a missing caller number is an empty string, never None",
          build_report_body(linkedid="x", caller_number=None, port="failed",
                            data={})["caller_number"] == "")
    check("an absent owen_url is None rather than an empty string",
          build_report_body(linkedid="x", caller_number="", port="failed",
                            data={}, owen_url="")["owen_url"] is None)
    check("the link is passed through when there is one",
          body["owen_url"] == "https://owen/c/1758.9")


def test_every_port_the_runtime_can_report_is_known():
    # `transferred` is not a session port: the runtime substitutes it once the caller has
    # actually been moved. If that vocabulary changes, this is where it is noticed.
    for port in ("transfer", "transferred", "end_call", "default", "failed"):
        check(f"{port!r} is a known outcome", port in KNOWN_OUTCOMES)


if __name__ == "__main__":
    test_a_transferred_call_says_where_it_went()
    test_the_bug_itself_an_unresolved_transfer_reports_no_destination()
    test_duration_is_unknown_rather_than_zero()
    test_captures_are_normalised_and_absent_when_empty()
    test_the_body_is_exactly_the_contract()
    test_every_port_the_runtime_can_report_is_known()
    print(f"\n{_checks - len(_failures)}/{_checks} checks passed")
    for f in _failures:
        print(f"  FAILED: {f}")
    raise SystemExit(1 if _failures else 0)

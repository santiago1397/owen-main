"""The event body OWEN sends must be one the CRM actually accepts.

Two layers:

  1. **Always**: the payloads `events.to_crm_event` builds are checked against the contract
     as this module records it — the field names, the four event types, the two directions,
     and the five call statuses `ghl-clone` will accept on a CALL.

  2. **When the CRM source is on this machine**: the contract itself is re-derived from
     `ghl-clone/backend/app/main.py` by parsing it with `ast` (stdlib — the CRM is not
     importable from here, it has its own virtualenv). That turns a contract DRIFT into a
     failing unit test rather than a 400 discovered in a retry loop against a live CRM.
     Skipped, loudly, when the source is not present.

     Looked for at $GHL_CLONE_PATH, then ../ghl-clone, then ~/ghl-clone.

The behavioural rule this file exists to protect is in `events.py`'s docstring and is worth
repeating: the CRM's `automations.on_inbound_call` fires the missed-call auto-text-back for
ANY inbound CALL row whose `duration_seconds` is <= 15 or absent. Reporting a call's START
as a CALL row would therefore tell the CRM every single call was missed and queue a text to
the customer while they were still on the line. Only the TERMINAL event is a CALL.

Run: python -m tests.test_crm_event_payload
"""

import ast
import os
import pathlib

OWEN_CALL_ID = "3f1e0c22-0000-4000-8000-000000000abc"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"crm_event_payload failed at: {name}")


def _facts(**kw):
    from app.integrations.crm.events import CallEventFacts

    base = dict(
        phase="ended", owen_call_id=OWEN_CALL_ID, linkedid="1799000333.9",
        caller_number="+15615559999", dialed_number="+15615550200",
        direction="inbound", outcome="answered", duration_seconds=134,
        winning_destination="+15615550111", winning_kind="pstn",
        recording_id="rec-1", transcript_id="tr-1",
    )
    base.update(kw)
    return CallEventFacts(**base)


# --- layer 1: the payloads we build ---------------------------------------------------------

def test_the_terminal_event_is_a_CALL_with_everything_on_it():
    print("the ENDED event is a CALL carrying duration, direction and status:")
    from app.integrations.crm.events import to_crm_event, validate_crm_event

    body = to_crm_event(_facts(), contact_id=41)
    check("the CRM would accept the shape", validate_crm_event(body) == [])
    check("type is CALL", body["type"] == "CALL")
    check("direction is INBOUND", body["direction"] == "INBOUND")
    check("duration is carried", body["duration_seconds"] == 134)
    check("an answered call maps to the CRM's 'completed'", body["call_status"] == "completed")
    check("contact_id is an int, as the CRM requires", isinstance(body["contact_id"], int))
    check("provider_ref carries calls.id — the CRM's owen_call_id join key",
          body["provider_ref"] == OWEN_CALL_ID)
    check("the winning destination is in the summary the CRM shows",
          "+15615550111" in body["body"])
    check("so is the recording id", "rec-1" in body["body"])
    check("so is the transcript id", "tr-1" in body["body"])
    check("and so is the join key, for a human reading the thread",
          OWEN_CALL_ID in body["body"])


def test_outcomes_map_onto_the_five_statuses_the_CRM_accepts():
    print("every ring outcome maps onto a status the CRM will accept:")
    from app.integrations.crm.events import (CRM_CALL_STATUSES, crm_call_status,
                                             to_crm_event, validate_crm_event)

    for outcome, expected in (("answered", "completed"), ("voicemail", "voicemail"),
                              ("noanswer", "no-answer"), ("busy", "busy"),
                              ("failed", "failed")):
        check(f"{outcome} -> {expected}", crm_call_status(outcome) == expected)
        body = to_crm_event(_facts(outcome=outcome), contact_id=1)
        check(f"  and {expected} passes validation", validate_crm_event(body) == [])

    check("every mapped status is in the accepted set",
          all(crm_call_status(o) in CRM_CALL_STATUSES
              for o in ("answered", "voicemail", "noanswer", "busy", "failed", "")))
    # An outcome we cannot account for must not be reported as a success: an over-reported
    # failure is a question somebody asks; an over-reported success is not.
    check("an UNKNOWN outcome degrades to 'failed', never 'completed'",
          crm_call_status("something-new") == "failed")


def test_started_and_answered_are_internal_notes_not_CALL_rows():
    """The rule that stops OWEN texting a customer who is still on the phone."""
    print("the pre-terminal phases are INTERNAL_COMMENT, so no automation fires:")
    from app.integrations.crm.events import to_crm_event, validate_crm_event

    for phase in ("started", "answered"):
        body = to_crm_event(_facts(phase=phase, duration_seconds=None), contact_id=7)
        check(f"{phase}: type is INTERNAL_COMMENT", body["type"] == "INTERNAL_COMMENT")
        check(f"{phase}: it carries NO call_status", "call_status" not in body)
        check(f"{phase}: it carries NO duration", "duration_seconds" not in body)
        check(f"{phase}: direction is OUTBOUND, so the unread badge is not bumped",
              body["direction"] == "OUTBOUND")
        check(f"{phase}: still valid", validate_crm_event(body) == [])
        check(f"{phase}: still carries the join key", body["provider_ref"] == OWEN_CALL_ID)


def test_a_short_call_is_reported_as_short_and_that_is_deliberate():
    """A genuinely missed call SHOULD trigger the CRM's text-back. What must not happen is
    a call being reported as missed while it is still in progress."""
    print("a real short/missed call reports its real duration on the terminal event:")
    from app.integrations.crm.events import (CRM_MISSED_CALL_MAX_SECONDS, to_crm_event)

    body = to_crm_event(_facts(outcome="noanswer", duration_seconds=6), contact_id=3)
    check("it is a CALL", body["type"] == "CALL")
    check("INBOUND", body["direction"] == "INBOUND")
    check("with the true duration", body["duration_seconds"] == 6)
    check("which is inside the CRM's missed-call window (so the text-back is intended)",
          body["duration_seconds"] <= CRM_MISSED_CALL_MAX_SECONDS)


def test_validation_catches_what_the_CRM_would_reject():
    print("validate_crm_event rejects exactly what the CRM rejects:")
    from app.integrations.crm.events import validate_crm_event

    ok = {"contact_id": 1, "type": "CALL", "direction": "INBOUND",
          "call_status": "completed", "duration_seconds": 10}
    check("a good body passes", validate_crm_event(ok) == [])
    check("a string contact_id is caught",
          validate_crm_event({**ok, "contact_id": "1"}) != [])
    check("an unknown type is caught", validate_crm_event({**ok, "type": "VOICE"}) != [])
    check("a bad direction is caught", validate_crm_event({**ok, "direction": "IN"}) != [])
    check("an unknown call_status is caught",
          validate_crm_event({**ok, "call_status": "missed"}) != [])
    check("call_status on a non-CALL is caught",
          validate_crm_event({**ok, "type": "SMS"}) != [])


def test_a_payload_survives_the_job_queue_round_trip():
    """A job payload is JSON in Postgres and may be drained by a newer deploy than wrote it."""
    print("facts survive the round trip through the jobs table:")
    import json

    from app.integrations.crm.events import CallEventFacts, to_crm_event

    facts = _facts()
    revived = CallEventFacts.from_payload(json.loads(json.dumps(facts.as_payload())))
    check("identical after a JSON round trip", revived.as_payload() == facts.as_payload())
    check("and builds the same CRM body",
          to_crm_event(revived, 5) == to_crm_event(facts, 5))
    check("an unknown/extra key does not break parsing",
          CallEventFacts.from_payload({"phase": "ended", "future_field": 1}).phase == "ended")
    check("a garbage duration degrades to None rather than raising",
          CallEventFacts.from_payload({"duration_seconds": "soon"}).duration_seconds is None)


# --- layer 2: re-derive the contract from the CRM's own source -------------------------------

def _find_crm_source():
    candidates = []
    env = os.environ.get("GHL_CLONE_PATH")
    if env:
        candidates.append(pathlib.Path(env))
    here = pathlib.Path(__file__).resolve()
    candidates.append(here.parents[2].parent / "ghl-clone")   # sibling of owen-main
    candidates.append(pathlib.Path.home() / "ghl-clone")
    for base in candidates:
        main = base / "backend" / "app" / "main.py"
        if main.is_file():
            return main
    return None


def _crm_contract(main_py: pathlib.Path):
    """Pull EventIngest's fields and CALL_STATUSES out of the CRM source with `ast`."""
    tree = ast.parse(main_py.read_text(encoding="utf-8"))
    fields, statuses = {}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "EventIngest":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields[stmt.target.id] = ast.unparse(stmt.annotation)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "CALL_STATUSES":
                    try:
                        statuses = set(ast.literal_eval(node.value))
                    except (ValueError, SyntaxError):
                        pass
    return fields, statuses


def test_the_contract_still_matches_the_crm_source():
    print("the CRM's own source still describes the contract this module targets:")
    main_py = _find_crm_source()
    if main_py is None:
        print("  [SKIP] ghl-clone source not found "
              "(set GHL_CLONE_PATH to enable this drift check)")
        return

    from app.integrations.crm.events import (CRM_CALL_STATUSES, CRM_TYPE_CALL,
                                             CRM_TYPE_INTERNAL, to_crm_event)

    fields, statuses = _crm_contract(main_py)
    check(f"EventIngest was found in {main_py}", bool(fields))
    check("contact_id is still a required int",
          fields.get("contact_id") == "int")
    for name in ("type", "direction", "body", "duration_seconds", "call_status",
                 "recording_url", "provider_ref"):
        check(f"the CRM still has a {name} field", name in fields)

    check("CALL_STATUSES was found", bool(statuses))
    check("this module's status vocabulary still matches the CRM's",
          CRM_CALL_STATUSES == frozenset(statuses))

    type_ann = fields.get("type", "")
    check("CALL is still an accepted event type", f"'{CRM_TYPE_CALL}'" in type_ann)
    check("INTERNAL_COMMENT is still an accepted event type",
          f"'{CRM_TYPE_INTERNAL}'" in type_ann)

    # Every key we send must be a field the CRM declares, or it is silently dropped (or,
    # once the CRM tightens `extra`, a 422).
    body = to_crm_event(_facts(), contact_id=1)
    unknown = sorted(set(body) - set(fields))
    check(f"we send no key the CRM does not declare (extras: {unknown})", unknown == [])


if __name__ == "__main__":
    test_the_terminal_event_is_a_CALL_with_everything_on_it()
    test_outcomes_map_onto_the_five_statuses_the_CRM_accepts()
    test_started_and_answered_are_internal_notes_not_CALL_rows()
    test_a_short_call_is_reported_as_short_and_that_is_deliberate()
    test_validation_catches_what_the_CRM_would_reject()
    test_a_payload_survives_the_job_queue_round_trip()
    test_the_contract_still_matches_the_crm_source()
    print("\nALL CRM EVENT-PAYLOAD CHECKS PASSED")

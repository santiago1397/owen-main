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

    # The AI-agent seam in handler.py is not wired, but its outcomes are mapped now: without
    # them, the day it IS wired every agent-handled call would report as `failed` and the
    # CRM's call report would say the phone system was broken.
    for outcome in ("agent", "transferred"):
        check(f"the unwired agent seam's {outcome!r} outcome is already mapped",
              crm_call_status(outcome) == "completed")

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


def test_an_unknown_caller_is_carried_by_from_number():
    """Gap 1. The single most valuable event this business gets is a first-time roofing
    lead calling in, and until the CRM's amendment it was the one event that could not be
    delivered: no contact to name, so nothing was sent.

    Both halves are asserted — the field is there, AND a body without a contact_id is a
    body this module considers sendable rather than malformed."""
    print("a caller with no CRM contact is carried by from_number, not dropped:")
    from app.integrations.crm.events import to_crm_event, validate_crm_event

    stranger = to_crm_event(_facts(caller_number="+15615559999"), contact_id=None)
    check("the body carries the caller's number", stranger["from_number"] == "+15615559999")
    check("and no contact_id at all, rather than a null or a guess",
          "contact_id" not in stranger)
    check("it is a sendable body, not a malformed one", validate_crm_event(stranger) == [])
    check("it is still the CALL row with everything on it",
          stranger["type"] == "CALL" and stranger["call_status"] == "completed")

    for phase in ("started", "answered"):
        note = to_crm_event(_facts(phase=phase), contact_id=None)
        check(f"{phase}: a stranger's pre-terminal note is deliverable too",
              validate_crm_event(note) == [] and note["from_number"] == "+15615559999")


def test_a_resolved_contact_still_sends_exactly_what_it_did_before():
    """The other half of Gap 1: the path that WORKS today must not change. A resolved
    contact gets the same body it always got, plus the one new field."""
    print("a resolved contact's body is unchanged apart from the new field:")
    from app.integrations.crm.events import to_crm_event

    known = to_crm_event(_facts(), contact_id=41)
    check("contact_id is still sent, and is still an int", known["contact_id"] == 41)
    check("from_number rides alongside it", known["from_number"] == "+15615559999")
    check("and nothing else about the body moved",
          {k: v for k, v in known.items() if k != "from_number"}
          == {"contact_id": 41, "body": known["body"],
              "provider_ref": OWEN_CALL_ID, "type": "CALL", "direction": "INBOUND",
              "call_status": "completed", "duration_seconds": 134,
              "recording_url": None})


def test_an_event_with_neither_a_contact_nor_a_number_is_refused_here():
    """The CRM answers 422 for this. Catching it locally keeps a payload we cannot file
    out of a five-attempt retry loop against a live CRM."""
    print("an event naming neither a contact nor a number is refused before it is sent:")
    from app.integrations.crm.events import to_crm_event, validate_crm_event

    nameless = to_crm_event(_facts(caller_number=""), contact_id=None)
    problems = validate_crm_event(nameless)
    check("it is refused", problems != [])
    check("and says what is missing",
          any("contact_id or a from_number" in p for p in problems))


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

    # THE AMENDMENT (CRM side, 2026-09-11): contact_id became optional and from_number was
    # added, which is what lets a first-time caller reach the CRM at all. The two contract
    # versions are BOTH recognised here on purpose — the source on this machine may be an
    # older checkout than the branch that carries the amendment, and a drift check that
    # cannot tell "older CRM" from "we broke the contract" is not worth running.
    amended = "from_number" in fields
    body = to_crm_event(_facts(), contact_id=1)
    unknown = sorted(set(body) - set(fields))

    if amended:
        check("contact_id is optional, so a stranger's call is no longer undeliverable",
              fields.get("contact_id") in ("int | None", "Optional[int]"))
        check("from_number is declared, and is what carries the stranger",
              "from_number" in fields)
        # Every key we send must be a field the CRM declares, or it is silently dropped
        # (or, once the CRM tightens `extra`, a 422).
        check(f"we send no key the CRM does not declare (extras: {unknown})",
              unknown == [])
        stranger = to_crm_event(_facts(), contact_id=None)
        check("and a body with no contact_id is still entirely declared fields",
              sorted(set(stranger) - set(fields)) == [])
    else:
        print("  [NOTE] this ghl-clone checkout PREDATES the contact_id amendment "
              f"({main_py}). owen-main is built for the amended contract; deploy the "
              "CRM half or an event without a contact_id is a 422 there.")
        check("the pre-amendment CRM still declares contact_id as a required int",
              fields.get("contact_id") == "int")
        check("and the ONLY key it does not declare is the one the amendment adds "
              f"(extras: {unknown})", unknown == ["from_number"])


if __name__ == "__main__":
    test_the_terminal_event_is_a_CALL_with_everything_on_it()
    test_outcomes_map_onto_the_five_statuses_the_CRM_accepts()
    test_started_and_answered_are_internal_notes_not_CALL_rows()
    test_a_short_call_is_reported_as_short_and_that_is_deliberate()
    test_an_unknown_caller_is_carried_by_from_number()
    test_a_resolved_contact_still_sends_exactly_what_it_did_before()
    test_an_event_with_neither_a_contact_nor_a_number_is_refused_here()
    test_validation_catches_what_the_CRM_would_reject()
    test_a_payload_survives_the_job_queue_round_trip()
    test_the_contract_still_matches_the_crm_source()
    print("\nALL CRM EVENT-PAYLOAD CHECKS PASSED")

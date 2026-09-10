"""Finding the CRM contact a call belongs to — the most fragile part of the link.

`POST /api/events` takes a `contact_id` and 404s on anything else. It has no phone lookup
and no create-on-ingest, so every call OWEN reports has to be matched to a contact first,
and two verified facts make that harder than it sounds:

  1. `Contact.phone` is a DISPLAY string. The CRM's own seed writes `"(941) 555-1234"`.
     OWEN holds E.164. `GET /api/contacts?q=` is a raw substring ILIKE over that column, so
     `q=+19415551234` matches NOTHING, ever — a naive lookup would silently resolve no
     caller at all and every call would be dropped with "no matching contact".
  2. That search needs the `read` scope. A token scoped `events:write` alone can POST
     /api/events and nothing else, so lookup 403s and must say so rather than guess an id.

And one judgement, which this file pins down: the match is EXACT on the last ten digits.
The ILIKE that finds a row is a substring match and will cheerfully return a different
customer whose number contains the same seven digits. Filing a call on the wrong person's
timeline is worse than filing it nowhere — the judgement `docs/CRM_CONTEXT_SPEC.md` C3 made
about greeting the wrong person by name.

Run: python -m tests.test_crm_contact_lookup
"""

import asyncio


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"crm_contact_lookup failed at: {name}")


class FakeCrm:
    """A CRM whose /api/contacts behaves like the real one: a substring ILIKE over a
    DISPLAY-formatted phone column."""

    def __init__(self, contacts, *, status=200):
        self.contacts = contacts          # [{"id":.., "phone": "(941) 555-1234"}, ...]
        self.status = status
        self.queries = []

    def make_client(self):
        from app.integrations.crm.client import CrmClient, CrmResult

        crm = self

        class Client(CrmClient):
            async def _request(self, method, path, **kwargs):
                q = (kwargs.get("params") or {}).get("q", "")
                crm.queries.append(q)
                if crm.status != 200:
                    return CrmResult(False, crm.status, "denied")
                items = [c for c in crm.contacts
                         if q.lower() in str(c.get("phone") or "").lower()]
                return CrmResult(True, 200, "", {"items": items})

        return Client("http://crm:8000", "ghl_pat_test")


def test_display_formatted_phones_are_found():
    """The bug this whole candidate list exists to avoid."""
    print("an E.164 caller is matched against a display-formatted CRM contact:")
    crm = FakeCrm([{"id": 17, "phone": "(941) 555-1234", "name": "Maria Santos"}])
    contact_id, reason = asyncio.run(
        crm.make_client().resolve_contact_id("+19415551234"))

    check("the contact was found", contact_id == 17)
    check("and reported as a match", reason == "matched")
    check("a raw E.164 query alone would have found nothing",
          "+19415551234" not in crm.queries[:1])


def test_other_stored_formats_also_resolve():
    print("the same caller resolves however the CRM happens to store the number:")
    for stored in ("(941) 555-1234", "941-555-1234", "9415551234",
                   "+19415551234", "1-941-555-1234"):
        crm = FakeCrm([{"id": 5, "phone": stored}])
        contact_id, _r = asyncio.run(crm.make_client().resolve_contact_id("+19415551234"))
        check(f"stored as {stored!r}", contact_id == 5)


def test_a_substring_hit_on_a_different_number_is_rejected():
    """The ILIKE is a substring match. Without the last-ten confirmation this would file the
    call on the wrong customer's timeline."""
    print("a substring hit that is NOT the same number is refused:")
    crm = FakeCrm([
        # Contains "555-1234" but is a different area code — a real ILIKE hit.
        {"id": 91, "phone": "(305) 555-1234", "name": "Someone Else"},
    ])
    contact_id, reason = asyncio.run(
        crm.make_client().resolve_contact_id("+19415551234"))

    check("no contact was returned", contact_id is None)
    check("the query DID hit that row (so the guard is what refused it)",
          any("555-1234" in q for q in crm.queries))
    check("and the reason names the number", "+19415551234" in reason)


def test_the_right_contact_wins_when_both_are_returned():
    print("when a query returns several rows, only the exact number is taken:")
    crm = FakeCrm([
        {"id": 91, "phone": "(305) 555-1234"},
        {"id": 92, "phone": "(941) 555-1234"},
    ])
    contact_id, _r = asyncio.run(crm.make_client().resolve_contact_id("+19415551234"))
    check("the matching contact was chosen", contact_id == 92)


def test_a_write_only_token_reports_the_missing_scope():
    """A token scoped `events:write` alone 403s on GET /api/contacts. That has to be a
    sentence somebody can act on, not a silent 'no matching contact'."""
    print("a token without the 'read' scope says so:")
    crm = FakeCrm([{"id": 1, "phone": "(941) 555-1234"}], status=403)
    contact_id, reason = asyncio.run(
        crm.make_client().resolve_contact_id("+19415551234"))

    check("no contact", contact_id is None)
    check("the reason names the scope", "read" in reason and "events:write" in reason)
    check("it gave up rather than trying every rendering", len(crm.queries) == 1)


def test_a_rejected_token_and_an_unreachable_crm_are_distinguished():
    print("a 401 and a dead CRM are reported differently:")
    crm401 = FakeCrm([], status=401)
    _cid, reason401 = asyncio.run(crm401.make_client().resolve_contact_id("+19415551234"))
    check("a 401 says the token was rejected", "401" in reason401)

    crm500 = FakeCrm([], status=500)
    _cid, reason500 = asyncio.run(crm500.make_client().resolve_contact_id("+19415551234"))
    check("a 5xx says the lookup failed", "lookup failed" in reason500)
    check("and it stopped rather than hammering the CRM with more queries",
          len(crm500.queries) == 1)


def test_an_unknown_caller_is_a_normal_outcome():
    print("an unknown caller is a reason, not an error — and never a new contact:")
    crm = FakeCrm([{"id": 1, "phone": "(941) 555-9999"}])
    contact_id, reason = asyncio.run(
        crm.make_client().resolve_contact_id("+19415551234"))
    check("nothing matched", contact_id is None)
    check("with a reason naming the caller", "+19415551234" in reason)
    check("and NO contact was created (the client has no create method)",
          not hasattr(crm.make_client(), "create_contact"))


def test_an_unusable_caller_number_is_refused_immediately():
    print("a blank or unusable caller number does not go near the CRM:")
    crm = FakeCrm([{"id": 1, "phone": "(941) 555-1234"}])
    contact_id, reason = asyncio.run(crm.make_client().resolve_contact_id(""))
    check("no contact", contact_id is None)
    check("and no query was made at all", crm.queries == [])
    check("with a reason", "empty" in reason or "unusable" in reason)


def test_candidate_renderings_are_ordered_narrowest_first():
    print("the narrowest query is tried first, so the usual case ends on one round trip:")
    from app.integrations.crm.client import phone_candidates

    candidates = phone_candidates("+19415551234")
    check("the local NNN-NNNN form is first (it matches the CRM's stored format)",
          candidates[0] == "555-1234")
    check("no duplicates", len(candidates) == len(set(candidates)))
    check("the fully-formatted form is offered", "(941) 555-1234" in candidates)
    check("so is bare 10-digit", "9415551234" in candidates)
    check("so is E.164", "+19415551234" in candidates)


if __name__ == "__main__":
    test_display_formatted_phones_are_found()
    test_other_stored_formats_also_resolve()
    test_a_substring_hit_on_a_different_number_is_rejected()
    test_the_right_contact_wins_when_both_are_returned()
    test_a_write_only_token_reports_the_missing_scope()
    test_a_rejected_token_and_an_unreachable_crm_are_distinguished()
    test_an_unknown_caller_is_a_normal_outcome()
    test_an_unusable_caller_number_is_refused_immediately()
    test_candidate_renderings_are_ordered_narrowest_first()
    print("\nALL CRM CONTACT-LOOKUP CHECKS PASSED")

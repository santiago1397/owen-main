"""Unit test for the BulkVS number-sync kernel (Ticket 03).

No API, no DB — the reconcile is a PURE planner (app.services.number_sync.plan_sync) over
the current rows vs. the incoming /tnRecord DIDs, so the locked add-only rules are proven
in isolation here against a FAKED /tnRecord response:
  - add-only INSERT of a brand-new DID;
  - SOFT-RELEASE of an active DID that vanished (row kept, not deleted);
  - REACTIVATE of a previously-released DID that reappeared (same row);
  - one-way ReferenceID -> friendly_name label MIRROR (incl. clearing);
  - DERIVED lifecycle (available / assigned / released).
Also parses a faked /tnRecord JSON body if the provider client imports cleanly (skipped,
not failed, when app deps like pydantic/httpx are absent in a bare sandbox).

Run: python -m tests.test_number_sync
"""

import sys
from types import SimpleNamespace

from app.services.number_sync import derive_lifecycle, is_carrier_active, plan_sync


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"number_sync failed at: {name}")


def _row(phone, *, label=None, active=True, released_at=None, provider_status=None):
    """A lightweight stand-in for a bulkvs-owned Number row (plan_sync is duck-typed)."""
    return SimpleNamespace(
        phone_number=phone, friendly_name=label, active=active, released_at=released_at,
        provider_status=provider_status,
    )


def _tn(phone, ref=None, status=None):
    return SimpleNamespace(phone_number=phone, reference_id=ref, status=status)


def main():
    print("derive_lifecycle — available / assigned / released:")
    check("active, no campaign/flow -> available",
          derive_lifecycle(active=True, released_at=None) == "available")
    check("active + campaign -> assigned",
          derive_lifecycle(active=True, released_at=None, campaign_id="c") == "assigned")
    check("active + flow -> assigned",
          derive_lifecycle(active=True, released_at=None, flow_id="f") == "assigned")
    check("released_at set -> released (dominates campaign)",
          derive_lifecycle(active=True, released_at="2026-01-01", campaign_id="c") == "released")
    check("inactive -> released",
          derive_lifecycle(active=False, released_at=None) == "released")
    check("carrier SUBMITTED -> pending (dominates assigned)",
          derive_lifecycle(active=True, released_at=None, campaign_id="c",
                           provider_status="SUBMITTED") == "pending")
    check("carrier Active -> normal derivation",
          derive_lifecycle(active=True, released_at=None, provider_status="Active")
          == "available")
    check("released dominates pending",
          derive_lifecycle(active=False, released_at=None, provider_status="SUBMITTED")
          == "released")

    print("is_carrier_active — SUBMITTED (and any non-Active) is not operable:")
    check("Active is operable", is_carrier_active("Active"))
    check("case-insensitive", is_carrier_active("ACTIVE") and is_carrier_active(" active "))
    check("SUBMITTED is NOT operable", not is_carrier_active("SUBMITTED"))
    check("any other status is NOT operable", not is_carrier_active("Pending"))
    check("NULL (legacy / pre-migration row) stays operable", is_carrier_active(None))

    print("plan_sync — add-only insert:")
    plan = plan_sync(existing=[], incoming=[_tn("+19195550001", "Roofing CL")])
    check("brand-new DID is inserted", [t.phone_number for t in plan.insert] == ["+19195550001"])
    check("nothing else happens", not (plan.reactivate or plan.relabel or plan.soft_release))

    print("plan_sync — soft-release on vanish:")
    existing = [_row("+19195550001", label="Roofing CL"), _row("+19195550002", label="HVAC")]
    plan = plan_sync(existing=existing, incoming=[_tn("+19195550001", "Roofing CL")])
    check("vanished active DID is soft-released",
          [r.phone_number for r in plan.soft_release] == ["+19195550002"])
    check("surviving DID with same label is untouched",
          not (plan.insert or plan.reactivate or plan.relabel))

    print("plan_sync — already-released DID stays gone (idempotent):")
    existing = [_row("+19195550002", active=False, released_at="2026-01-01")]
    plan = plan_sync(existing=existing, incoming=[])
    check("released + still-gone DID is NOT re-released", not plan.soft_release)

    print("plan_sync — reactivate on return:")
    existing = [_row("+19195550002", label="HVAC", active=False, released_at="2026-01-01")]
    plan = plan_sync(existing=existing, incoming=[_tn("+19195550002", "HVAC Spring")])
    check("returning DID reactivates the SAME row",
          len(plan.reactivate) == 1 and plan.reactivate[0][0] is existing[0])
    check("reactivate carries the fresh label",
          plan.reactivate[0][1].reference_id == "HVAC Spring")
    check("reactivate is not also an insert", not plan.insert)

    print("plan_sync — one-way label mirror:")
    existing = [_row("+19195550001", label="old note")]
    plan = plan_sync(existing=existing, incoming=[_tn("+19195550001", "new note")])
    check("changed ReferenceID triggers relabel",
          plan.relabel == [(existing[0], "new note")])
    # Unchanged label => no churn.
    plan = plan_sync(existing=[_row("+1", label="x")], incoming=[_tn("+1", "x")])
    check("unchanged label -> no relabel", not plan.relabel)
    # Removed note in the portal mirrors to NULL.
    existing = [_row("+1", label="x")]
    plan = plan_sync(existing=existing, incoming=[_tn("+1", None)])
    check("cleared ReferenceID mirrors to None", plan.relabel == [(existing[0], None)])

    print("plan_sync — one-way carrier-status mirror:")
    existing = [_row("+19195550001", provider_status="SUBMITTED")]
    plan = plan_sync(existing=existing, incoming=[_tn("+19195550001", status="Active")])
    check("changed Status triggers restatus (port-in completed)",
          plan.restatus == [(existing[0], "Active")])
    plan = plan_sync(existing=[_row("+1", provider_status="Active")],
                     incoming=[_tn("+1", status="Active")])
    check("unchanged Status -> no restatus churn", not plan.restatus)
    existing = [_row("+1", label="a", provider_status="Active")]
    plan = plan_sync(existing=existing, incoming=[_tn("+1", "b", status="SUBMITTED")])
    check("relabel and restatus can both apply to one row",
          plan.relabel == [(existing[0], "b")]
          and plan.restatus == [(existing[0], "SUBMITTED")])

    print("plan_sync — ported DID adopts the legacy (foreign-provider) row instead of duplicating:")
    legacy = _row("+19195550009", label="GBP Legacy Twilio")
    plan = plan_sync(existing=[], incoming=[_tn("+19195550009", "Ported Note")], foreign=[legacy])
    check("ported DID is adopted, not inserted",
          len(plan.adopt) == 1 and plan.adopt[0][0] is legacy and not plan.insert)
    check("adopt carries the fresh label", plan.adopt[0][1].reference_id == "Ported Note")

    print("plan_sync — rows are inspected, never mutated:")
    row = _row("+19195550002", label="HVAC")
    plan_sync(existing=[row], incoming=[])  # would soft-release
    check("planner did not mutate the row", row.active is True and row.released_at is None)

    print("bulkvs_client.parse_tn_records — faked /tnRecord body:")
    try:
        from app.providers.bulkvs_client import parse_tn_records
    except Exception as exc:  # noqa: BLE001 - app deps (pydantic/httpx) absent in bare sandbox
        print(f"  [SKIP] provider client import unavailable: {exc.__class__.__name__}")
    else:
        fake = [
            {"TN": "9195550001", "ReferenceID": "Roofing CL", "Status": "Active"},  # 10-digit -> +1
            {"TN": "19195550002", "ReferenceID": ""},            # 11-digit, blank ref -> None
            {"Number": "+19195550003", "Status": "SUBMITTED"},   # alias field, no ref
            {"ReferenceID": "orphan"},                            # no TN -> skipped
        ]
        tns = parse_tn_records(fake)
        check("skips record with no TN", len(tns) == 3)
        check("10-digit normalized to E.164", tns[0].phone_number == "+19195550001")
        check("ReferenceID mirrored as label", tns[0].reference_id == "Roofing CL")
        check("11-digit normalized + blank ref -> None",
              tns[1].phone_number == "+19195550002" and tns[1].reference_id is None)
        check("alias TN field parsed", tns[2].phone_number == "+19195550003")
        check("Status mirrored verbatim", tns[0].status == "Active")
        check("absent Status -> None", tns[1].status is None)
        check("SUBMITTED Status parsed", tns[2].status == "SUBMITTED")
        check("wrapped {'TNs': [...]} body also parsed",
              [t.phone_number for t in parse_tn_records({"TNs": fake})] ==
              [t.phone_number for t in tns])

    print("\nALL NUMBER-SYNC CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)

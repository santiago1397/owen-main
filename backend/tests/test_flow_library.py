"""Unit test for the flow-library kernels: clone source selection + delete planning.

Dependency-free (like test_number_flow_assignment): `pick_clone_source` and
`flow_delete_plan` are the pure kernels the /api/flows endpoints apply. The endpoints load
the rows; these functions decide — so the rules are proven without FastAPI or a DB.

Asserts:
- clone copies the ACTIVE version, even when a newer unactivated draft exists (the whole
  point: a clone reproduces what answers calls today, not someone's half-finished canvas);
- a never-activated flow falls back to its LATEST version, picked by version NUMBER rather
  than row order;
- a flow with no versions yields None (-> the endpoint's 422, not an empty copy);
- a dangling active_version_id (pointer to a version that isn't in the list) still resolves
  to the latest rather than returning nothing;
- delete REFUSES while any number is assigned, and that check outranks the call count;
- a flow with attributed calls ARCHIVES (never hard-deleted — calls.flow_version_id is
  call attribution behind a NO ACTION FK);
- only an unassigned, never-used flow is really DELETED.

Run: python -m tests.test_flow_library
"""

from app.flows.service import flow_delete_plan, pick_clone_source

V1, V2, V3 = "ver-1", "ver-2", "ver-3"


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"flow_library failed at: {name}")


def test_clone_prefers_active():
    print("clone source — the ACTIVE version wins over a newer draft:")
    # v3 is the latest saved version, but v2 is what actually answers calls.
    versions = [(V1, 1), (V2, 2), (V3, 3)]
    check("active (v2) chosen over latest (v3)",
          pick_clone_source(active_version_id=V2, versions=versions) == V2)
    check("active == latest is still the active one",
          pick_clone_source(active_version_id=V3, versions=versions) == V3)


def test_clone_falls_back_to_latest():
    print("clone source — never-activated flow falls back to the latest version:")
    # Deliberately out of order: the fallback must pick by version NUMBER, not list order.
    versions = [(V3, 3), (V1, 1), (V2, 2)]
    check("no active pointer -> highest version number",
          pick_clone_source(active_version_id=None, versions=versions) == V3)
    check("single version flow -> that version",
          pick_clone_source(active_version_id=None, versions=[(V1, 1)]) == V1)


def test_clone_with_nothing_to_copy():
    print("clone source — nothing to clone:")
    check("no versions at all -> None (endpoint 422s)",
          pick_clone_source(active_version_id=None, versions=[]) is None)
    check("no versions but a stale active pointer -> still None",
          pick_clone_source(active_version_id=V1, versions=[]) is None)
    # A pointer to a version that isn't among this flow's rows must not swallow the clone.
    check("dangling active pointer -> falls back to latest",
          pick_clone_source(active_version_id="ghost", versions=[(V1, 1), (V2, 2)]) == V2)


def test_delete_refuses_while_assigned():
    print("delete plan — assigned numbers block everything:")
    outcome, msg = flow_delete_plan(assigned_numbers=["+15618788090"], attributed_call_count=0)
    check("assigned -> refused", outcome == "refused")
    check("message names the number", "+15618788090" in (msg or ""))
    # Precedence matters: a never-used flow that is still assigned must NOT be deleted.
    outcome2, _ = flow_delete_plan(
        assigned_numbers=["+15618788090", "+16452516222"], attributed_call_count=0
    )
    check("assignment check outranks the call count", outcome2 == "refused")


def test_delete_archives_when_calls_attributed():
    print("delete plan — attributed calls force an archive:")
    outcome, msg = flow_delete_plan(assigned_numbers=[], attributed_call_count=2)
    check("has call history -> archived", outcome == "archived")
    check("no error message on archive", msg is None)
    check("even one attributed call is enough",
          flow_delete_plan(assigned_numbers=[], attributed_call_count=1)[0] == "archived")


def test_delete_removes_unused_flow():
    print("delete plan — unassigned + never used is really deleted:")
    outcome, msg = flow_delete_plan(assigned_numbers=[], attributed_call_count=0)
    check("clean flow -> deleted", outcome == "deleted")
    check("no error message on delete", msg is None)


if __name__ == "__main__":
    test_clone_prefers_active()
    test_clone_falls_back_to_latest()
    test_clone_with_nothing_to_copy()
    test_delete_refuses_while_assigned()
    test_delete_archives_when_calls_attributed()
    test_delete_removes_unused_flow()
    print("\nALL FLOW LIBRARY CHECKS PASSED")

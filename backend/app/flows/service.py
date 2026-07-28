"""Append-only version service for flows.

`flow_versions` are immutable by construction: saving a flow NEVER mutates an existing
version row, it always INSERTs a new one whose `version` is one past the current max.
`next_version_number` is the pure kernel of that rule (no DB import) so the append-only
behaviour can be unit-tested in isolation; the router calls it before inserting.

`flow_assignment_error` (Ticket 15.5) is the pure guard kernel for assigning a flow to a
number — same pattern: the PATCH /api/numbers/{id} endpoint loads the rows and this
function decides, so the assignment rules are unit-testable without FastAPI or a DB.

`pick_clone_source` and `flow_delete_plan` are the same pattern again, for the flow-library
actions: which version a clone copies, and whether deleting a flow may really remove it.
"""

from collections.abc import Iterable, Sequence
from typing import Optional


def next_version_number(existing_versions: Iterable[int]) -> int:
    """The version number for the next saved version.

    Versions are 1-based and monotonically increasing. Given the version numbers that
    already exist for a flow, the next one is max+1 (or 1 for the very first save). This
    is a pure function of the existing numbers — it neither reads nor mutates any row.
    """
    nums = list(existing_versions)
    return (max(nums) + 1) if nums else 1


def flow_assignment_error(
    *,
    number_media_provider: Optional[str],
    expected_media_provider: str,
    flow_exists: bool,
    flow_active_version_id: object,
) -> Optional[str]:
    """Why a flow may NOT be assigned to a number, or None when assignment is allowed.

    Pure kernel of the Ticket 15.5 PATCH guard (unassignment — flow_id null — is always
    allowed and never consults this):
    - only numbers whose media rides on the Asterisk platform accept a flow (the runtime
      resolves flows by (phone_number, media_provider) — assigning one anywhere else could
      never execute);
    - the flow must exist and have an ACTIVE version (a draft-only flow has nothing the
      runtime could run — activation, not assignment, is the go-live gate).
    """
    if (number_media_provider or "") != expected_media_provider:
        return (
            f"only numbers with media_provider '{expected_media_provider}' can be "
            "assigned a flow"
        )
    if not flow_exists:
        return "flow not found"
    if flow_active_version_id is None:
        return "flow has no active version; activate a version before assigning"
    return None


def pick_clone_source(
    *, active_version_id: object, versions: Sequence[tuple[object, int]]
) -> object:
    """Which version's graph a clone copies: the ACTIVE one, else the LATEST saved.

    `versions` is (version_id, version_number) pairs for the flow, any order. Returns the
    chosen version_id, or None when the flow has no version at all (nothing to clone —
    the endpoint turns that into a 422 rather than making an empty copy).

    Preferring the ACTIVE version means a clone reproduces what actually answers calls
    today, not an unactivated draft someone left half-finished on the canvas. The fallback
    to latest exists only so a never-activated flow is still clonable.
    """
    if active_version_id is not None and any(vid == active_version_id for vid, _ in versions):
        return active_version_id
    if not versions:
        return None
    return max(versions, key=lambda pair: pair[1])[0]


def flow_delete_plan(
    *, assigned_numbers: Sequence[str], attributed_call_count: int
) -> tuple[str, Optional[str]]:
    """What DELETE /api/flows/{id} may do: ("refused"|"archived"|"deleted", message).

    Two facts decide it, in this order:
    - ASSIGNED NUMBERS BLOCK EVERYTHING. A hard delete would violate `fk_numbers_flow_id`,
      and archiving an assigned flow would leave it hidden but STILL ANSWERING CALLS.
      Unassigning here instead would silently repoint a live DID at default handling, so
      the operator is made to do that explicitly.
    - ATTRIBUTED CALLS FORBID A HARD DELETE. `calls.flow_version_id` pins each call to the
      version that handled it, so removing the versions would either violate that NO ACTION
      FK or erase call attribution. Archive instead, keeping every reference valid.
    Only a flow that is unassigned AND has never handled a call is really deleted.
    """
    if assigned_numbers:
        return (
            "refused",
            f"still assigned to {', '.join(assigned_numbers)} — unassign it first",
        )
    if attributed_call_count > 0:
        return ("archived", None)
    return ("deleted", None)

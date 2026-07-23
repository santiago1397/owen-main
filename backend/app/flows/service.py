"""Append-only version service for flows.

`flow_versions` are immutable by construction: saving a flow NEVER mutates an existing
version row, it always INSERTs a new one whose `version` is one past the current max.
`next_version_number` is the pure kernel of that rule (no DB import) so the append-only
behaviour can be unit-tested in isolation; the router calls it before inserting.

`flow_assignment_error` (Ticket 15.5) is the pure guard kernel for assigning a flow to a
number — same pattern: the PATCH /api/numbers/{id} endpoint loads the rows and this
function decides, so the assignment rules are unit-testable without FastAPI or a DB.
"""

from collections.abc import Iterable
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

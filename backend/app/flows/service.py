"""Append-only version service for flows.

`flow_versions` are immutable by construction: saving a flow NEVER mutates an existing
version row, it always INSERTs a new one whose `version` is one past the current max.
`next_version_number` is the pure kernel of that rule (no DB import) so the append-only
behaviour can be unit-tested in isolation; the router calls it before inserting.
"""

from collections.abc import Iterable


def next_version_number(existing_versions: Iterable[int]) -> int:
    """The version number for the next saved version.

    Versions are 1-based and monotonically increasing. Given the version numbers that
    already exist for a flow, the next one is max+1 (or 1 for the very first save). This
    is a pure function of the existing numbers — it neither reads nor mutates any row.
    """
    nums = list(existing_versions)
    return (max(nums) + 1) if nums else 1

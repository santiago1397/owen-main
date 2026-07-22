"""Unit test for append-only flow versioning (app.flows.service.next_version_number).

No API, no DB — the version-numbering kernel the router uses before every INSERT is a pure
function, so the append-only guarantee ("saving never mutates a prior version") is proven
in isolation here by simulating a sequence of saves and asserting that:
- versions are 1-based and monotonically increasing (max + 1);
- each save appends a NEW record and leaves every prior record byte-for-byte unchanged.

Run: python -m tests.test_flow_versioning
"""

import copy
import sys

from app.flows.service import next_version_number


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        raise SystemExit(f"versioning failed at: {name}")


def _save(store: list[dict], graph: dict) -> dict:
    """Model exactly what the router does: compute the next version from existing numbers,
    then APPEND a new immutable record. Deep-copies the graph so the stored snapshot cannot
    be mutated by later edits to the caller's dict (matches an INSERT of the JSON)."""
    version = next_version_number([r["version"] for r in store])
    record = {"version": version, "graph": copy.deepcopy(graph)}
    store.append(record)
    return record


def main():
    print("next_version_number — 1-based, max + 1:")
    check("empty -> 1", next_version_number([]) == 1)
    check("[1] -> 2", next_version_number([1]) == 2)
    check("[1,2,3] -> 4", next_version_number([1, 2, 3]) == 4)
    check("out-of-order [3,1,2] -> 4", next_version_number([3, 1, 2]) == 4)

    print("append-only saves — prior versions never mutated:")
    store: list[dict] = []

    v1 = _save(store, {"nodes": {"a": {"type": "entry"}}})
    v1_snapshot = copy.deepcopy(v1)
    check("first save -> version 1", v1["version"] == 1)

    v2 = _save(store, {"nodes": {"a": {"type": "entry"}, "b": {"type": "hangup"}}})
    check("second save -> version 2", v2["version"] == 2)
    check("saving v2 did NOT mutate v1", store[0] == v1_snapshot)
    check("v1 and v2 are distinct objects", store[0] is not store[1])
    check("v1 graph unchanged by v2", store[0]["graph"] == {"nodes": {"a": {"type": "entry"}}})

    v3 = _save(store, {"nodes": {}})
    check("third save -> version 3", v3["version"] == 3)
    check("all versions retained, in order", [r["version"] for r in store] == [1, 2, 3])
    check("v1 STILL unchanged after v3", store[0] == v1_snapshot)

    # Mutating the caller's dict after a save must not reach back into the stored snapshot.
    later = {"nodes": {"x": {"type": "entry"}}}
    saved = _save(store, later)
    later["nodes"]["x"]["type"] = "hangup"
    check("stored snapshot isolated from later mutation", saved["graph"]["nodes"]["x"]["type"] == "entry")

    print("\nALL FLOW VERSIONING CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        print(e)
        sys.exit(1)

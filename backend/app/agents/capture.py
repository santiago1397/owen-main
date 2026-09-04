"""Capture-field normalisation — PURE, stdlib only (AI_AGENT_SPEC D7).

Split out of app/flows/runtime.py for the same reason as app/flows/transfer.py: runtime
imports httpx and sqlalchemy, so anything living there drags a live-call dependency into
every consumer — including the HTTP router, which has no business importing the ARI path.

The SHARED CORE vocabulary is the point. Anything outside it is preserved verbatim under
`extra`, mirroring the split `call_analysis` already uses (a controlled `category` alongside
free-form `tags`). Ten agents inventing ten spellings of "customer name" would make
cross-agent reporting impossible; a rigid schema would stop a specialist recording what it
actually needs.
"""

from __future__ import annotations

CAPTURE_CORE_FIELDS = ("name", "phone", "email", "address", "intent", "urgency", "notes")


def normalise_capture(raw: dict) -> dict:
    """Split an agent's captured payload into the shared core plus `extra`.

    Empty values are dropped rather than stored: a field the agent did not learn is absent,
    not blank, and storing "" would make "did we get an address?" unanswerable.
    """
    core, extra = {}, {}
    for key, value in (raw or {}).items():
        if value in (None, ""):
            continue
        k = str(key).strip().lower()
        if k in CAPTURE_CORE_FIELDS:
            core[k] = value
        else:
            extra[str(key)] = value
    if extra:
        core["extra"] = extra
    return core

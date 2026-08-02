"""The response shape every AI-API endpoint returns.

Machine callers cannot ask a follow-up question, so the envelope front-loads everything an
answer needs to be *quoted safely*:

    {
      "summary": "412 calls Jul 25-Aug 1 (America/New_York); 88 were under 45s.",
      "data": {...},
      "applied_filters": {...},   # exactly what was counted, with resolved UTC bounds
      "notes": [...]              # caveats that would otherwise be silently wrong
    }

`summary` exists so a literal model can restate the answer verbatim and still be correct.
`applied_filters` and `notes` exist because OWEN's call table contains rows that are not calls
(see NOTE_PHANTOM below) — a number quoted without its filter is a wrong number.

Errors use the same reflex: they name the problem, list the valid values, and suggest the fix.
"""

from __future__ import annotations

from typing import Any

# Caveats attached at the point of use, not just buried in the docs. These are the two facts
# that most reliably make an AI misreport OWEN's data.
NOTE_PHANTOM = (
    "Rows with no started_at are ingestion artifacts, not calls, and are always excluded "
    "(there are tens of thousands of them; a raw COUNT(*) on `calls` is not call volume)."
)
NOTE_JUNK = (
    "Junk calls (duration <= 13s, or never connected: failed/busy/no-answer/canceled) are "
    "excluded. Pass include_junk=true to count them."
)
NOTE_JUNK_INCLUDED = (
    "Junk calls (<= 13s or never connected) ARE included in these numbers, so they will read "
    "higher than the OWEN dashboard, which hides them by default."
)
NOTE_SPAM_DEAD = (
    "The LLM spam classifier is effectively dead data — only ~25 of 30k+ calls were ever "
    "flagged. Do not use is_spam to measure call quality; use duration and status instead."
)


def ok(
    summary: str,
    data: Any,
    applied_filters: dict | None = None,
    notes: list[str] | None = None,
) -> dict:
    return {
        "summary": summary,
        "data": data,
        "applied_filters": applied_filters or {},
        "notes": notes or [],
    }


def error_detail(code: str, message: str, hint: str | None = None, **extra: Any) -> dict:
    """The body of a 4xx/5xx. `hint` is what the caller should do next, in plain language."""
    body: dict[str, Any] = {"error": code, "message": message}
    if hint:
        body["hint"] = hint
    body.update(extra)
    return body

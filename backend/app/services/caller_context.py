"""OWEN's own half of the caller context (CRM_CONTEXT_SPEC C3/C6, build step 2).

This is the part that needs no CRM at all. `callers` holds 31,663 calls of history keyed by
E.164, and `call_captures` holds what agents have already learned — so an agent can open with
"welcome back" before any external system is involved, with no latency and no failure mode.
That is why the spec resolves OWEN FIRST and the provider second.

DB-aware (it queries), so the pure rules live in app/agents/context.py instead. Every function
here is best-effort: context is an enhancement, and a caller must be answered whether or not
we manage to work out who they are.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.agents.context import normalise_phone
from app.models import Call, CallCapture, Caller

logger = logging.getLogger("services.caller_context")

# Captures from older calls that still describe the same person. Two is enough to say
# something useful without the blob turning into a file on them.
RECENT_CAPTURE_LIMIT = 2


def _humanise_gap(then: datetime | None) -> str | None:
    if then is None:
        return None
    now = datetime.now(timezone.utc)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    days = (now - then).days
    if days <= 0:
        return "earlier today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return f"{days // 30} months ago"


async def local_context(db, caller_number: str) -> dict:
    """What OWEN already knows about this caller.

    Returns `{display_name, history, captures}` — or `{}` when the number is unusable or
    unknown. `display_name` comes only from `callers.label`, a MANUAL field: OWEN wins on
    identity precisely because a human typed that (C9), and inventing a name from anywhere
    else would defeat the point.
    """
    key = normalise_phone(caller_number)
    if len(key) != 10:
        return {}

    try:
        # Match on the last 10 digits so +1 / 1 / bare-10 and any formatting all collapse (C3).
        caller = (
            await db.execute(
                select(Caller)
                .where(func.right(func.regexp_replace(Caller.phone_number, r"\D", "", "g"), 10) == key)
                .limit(1)
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - never fail a call over context
        logger.exception("caller_context: lookup failed for %s", key)
        return {}

    if caller is None:
        return {}

    out: dict = {}
    if (caller.label or "").strip():
        out["display_name"] = caller.label.strip()

    # "Rung us 4 times, last time 3 days ago" is the single most useful thing OWEN knows and
    # no CRM has to be involved to say it.
    bits = []
    if caller.total_calls and caller.total_calls > 1:
        bits.append(f"has called {caller.total_calls} times before")
    last = await db.scalar(
        select(func.max(Call.started_at)).where(Call.caller_id == caller.id)
    )
    gap = _humanise_gap(last)
    if gap and caller.total_calls and caller.total_calls > 1:
        bits.append(f"most recently {gap}")
    if bits:
        out["history"] = "This caller " + ", ".join(bits) + "."

    # What agents previously captured about them — so a second call does not ask the same
    # questions a first one already answered.
    try:
        rows = (
            await db.execute(
                select(CallCapture)
                .join(Call, Call.id == CallCapture.call_id)
                .where(Call.caller_id == caller.id)
                .order_by(CallCapture.captured_at.desc())
                .limit(RECENT_CAPTURE_LIMIT)
            )
        ).scalars().all()
    except Exception:  # noqa: BLE001
        rows = []
    merged: dict = {}
    for row in reversed(rows):          # oldest first, so newer values win
        for k, v in (row.fields or {}).items():
            if k != "extra" and v not in (None, ""):
                merged[k] = v
    if merged:
        out["captures"] = merged
        if "display_name" not in out and merged.get("name"):
            # A name the agent captured is better than nothing, but it is still a MODEL's
            # transcription of speech — used only when no human has set a label.
            out["display_name"] = str(merged["name"])

    return out

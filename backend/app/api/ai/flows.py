"""What the call flows actually DID — the AI API's view of IVR behaviour.

Until this endpoint existed there was no way to ask "why are callers not reaching anyone?"
without hand-writing a window function over `call_events` and then opening the pinned
`flow_versions.graph` JSON to learn what each port meant. That gap hid a real outcome for
two weeks: on the flow serving every live BulkVS DID, the menu's `timeout` and `invalid`
ports both pointed at a hangup node, so roughly half of all inbound callers were hung up on
— and every one of those calls looked like an ordinary `completed` call in the dashboard.

The two event streams this reads are written by app/flows/interpreter.py:

- `flow.call.summary` — exactly one per call: the node path, the terminal node, and `ended`
  (the reason the flow stopped). `unrouted_hangup` is the one that matters: an unwired or
  errored port with no `default_fallback`, i.e. the caller was DROPPED rather than routed.
- `flow.node.exit` — one per node left, carrying the port taken, where it routed, how long
  the node took, and node-specific detail (menu digits, dial result, hours open/closed).

NOT RETROACTIVE. Both event types ship with this change, so windows reaching back before it
deployed will report fewer calls than actually ran. That is stated in `notes` on every
response rather than left for the caller to discover, because a machine caller reading "0
dropped calls" for last month would otherwise quote it as good news.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ai import periods
from app.api.ai.deps import AuthedKey, require_scope, resolve_window
from app.api.ai.envelope import NOTE_PHANTOM, ok
from app.api.ai.filters import REAL_CALL
from app.core.apikeys import SCOPE_READ
from app.db import get_db
from app.models import Call, CallEvent, Flow, FlowVersion

router = APIRouter(prefix="/api/ai", tags=["ai"])

SUMMARY_EVENT = "flow.call.summary"
EXIT_EVENT = "flow.node.exit"

NOTE_NOT_RETROACTIVE = (
    "Flow outcome events only exist for calls placed AFTER this instrumentation deployed. A "
    "window reaching further back will under-count: absence of dropped calls in an older "
    "period means no data, not no problem."
)
NOTE_DROPPED = (
    "'dropped' counts calls whose flow ended in unrouted_hangup — an unwired or errored port "
    "with no default_fallback, so the caller was hung up on rather than routed to voicemail "
    "or an operator. These still appear as ordinary completed calls everywhere else in OWEN."
)

# `payload -> 'flow' ->> '<key>'`: the interpreter nests every field under a "flow" object.
def _f(key: str):
    return CallEvent.payload["flow"][key].astext


def _pct(n: int, total: int) -> float:
    return round(100.0 * n / total, 1) if total else 0.0


@router.get("/flows/outcomes")
async def flow_outcomes(
    period: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    flow: str | None = Query(None, description="Restrict to one flow by name (exact match)."),
    db: AsyncSession = Depends(get_db),
    _: AuthedKey = Depends(require_scope(SCOPE_READ)),
) -> dict:
    """How calls moved through the IVR: where they ended, and why.

    Answers, for a period: how many flow-run calls there were, how they ended, which menu
    ports callers actually took (`timeout` = heard the prompt and pressed nothing; `invalid`
    = pressed an unwired key), what the dial attempts returned, and how many callers were
    dropped by an unwired port. The dropped count is the headline — it is invisible in every
    other view, because a dropped caller is still a `completed` call.
    """
    start, end, described = resolve_window(period, date_from, date_to)

    # Junk filtering is deliberately NOT applied. A caller hung up on by the IVR after 6
    # seconds IS junk by the dashboard's duration rule — and is exactly the call this endpoint
    # exists to surface. Filtering it out would hide the entire finding.
    base = [REAL_CALL, Call.started_at >= start, Call.started_at < end]
    if flow:
        base.append(Flow.name == flow)

    def _scoped(event_type: str):
        """Events of one type, joined to their call (+ the flow version pinned on it)."""
        q = (
            select(CallEvent)
            .join(Call, Call.id == CallEvent.call_id)
            .outerjoin(FlowVersion, FlowVersion.id == Call.flow_version_id)
            .outerjoin(Flow, Flow.id == FlowVersion.flow_id)
            .where(CallEvent.event_type == event_type, *base)
        )
        return q.subquery()

    summaries = _scoped(SUMMARY_EVENT)
    total = (await db.execute(
        select(func.count()).select_from(summaries)
    )).scalar_one()

    # --- how calls ended -----------------------------------------------------------------
    ended_col = summaries.c.payload["flow"]["ended"].astext
    ended_rows = (await db.execute(
        select(ended_col.label("ended"), func.count())
        .select_from(summaries).group_by("ended").order_by(func.count().desc())
    )).all()
    ended = [{"ended": e or "(unknown)", "calls": n, "pct": _pct(n, total)} for e, n in ended_rows]
    dropped = sum(n for e, n in ended_rows if e == "unrouted_hangup")

    terminal_col = summaries.c.payload["flow"]["terminal_node"].astext
    terminal_rows = (await db.execute(
        select(terminal_col.label("node"), func.count())
        .select_from(summaries).group_by("node").order_by(func.count().desc()).limit(25)
    )).all()

    # --- per-flow breakdown --------------------------------------------------------------
    per_flow_rows = (await db.execute(
        select(
            func.coalesce(Flow.name, "(no flow pinned)").label("flow"),
            func.count(),
            func.count().filter(
                CallEvent.payload["flow"]["ended"].astext == "unrouted_hangup"
            ),
        )
        .select_from(CallEvent)
        .join(Call, Call.id == CallEvent.call_id)
        .outerjoin(FlowVersion, FlowVersion.id == Call.flow_version_id)
        .outerjoin(Flow, Flow.id == FlowVersion.flow_id)
        .where(CallEvent.event_type == SUMMARY_EVENT, *base)
        .group_by("flow").order_by(func.count().desc())
    )).all()

    # --- menu + dial outcomes from the per-node exit events -------------------------------
    exits = _scoped(EXIT_EVENT)
    menu_rows = (await db.execute(
        select(
            exits.c.payload["flow"]["node_id"].astext.label("node"),
            exits.c.payload["flow"]["port"].astext.label("port"),
            exits.c.payload["flow"]["routed"].astext.label("routed"),
            func.count(),
        )
        .select_from(exits)
        .where(exits.c.payload["flow"]["node_type"].astext == "menu")
        .group_by("node", "port", "routed").order_by(func.count().desc()).limit(50)
    )).all()
    menu_total = sum(n for *_, n in menu_rows)

    dial_rows = (await db.execute(
        select(
            exits.c.payload["flow"]["dial_result"].astext.label("result"),
            func.count(),
        )
        .select_from(exits)
        .where(exits.c.payload["flow"]["node_type"].astext == "dial")
        .group_by("result").order_by(func.count().desc())
    )).all()

    notes = [NOTE_NOT_RETROACTIVE, NOTE_DROPPED, NOTE_PHANTOM,
             "Junk/short-call filtering is NOT applied here: a caller the IVR hung up on is a "
             "short call by definition, and excluding them would hide the outcome being measured."]
    if total == 0:
        notes.append(
            "No flow outcome events in this window. Either no calls ran a flow, or the window "
            "predates the instrumentation."
        )

    reached = total - dropped
    return ok(
        summary=(
            f"{total} flow-run calls {periods.describe_window(described)}; "
            f"{dropped} ({_pct(dropped, total)}%) were DROPPED by an unwired port with no "
            f"fallback, {reached} were routed."
        ),
        data={
            "calls_with_flow_data": total,
            "dropped": {"calls": dropped, "pct": _pct(dropped, total)},
            "ended": ended,
            "terminal_nodes": [
                {"node_id": n or "(none)", "calls": c, "pct": _pct(c, total)}
                for n, c in terminal_rows
            ],
            "flows": [
                {"flow": f, "calls": c, "dropped": d, "dropped_pct": _pct(d, c)}
                for f, c, d in per_flow_rows
            ],
            "menu_outcomes": [
                {"node_id": n, "port": p or "(none)", "routed": r,
                 "count": c, "pct": _pct(c, menu_total)}
                for n, p, r, c in menu_rows
            ],
            "dial_outcomes": [
                {"result": r or "(none)", "count": c} for r, c in dial_rows
            ],
        },
        applied_filters={**described, "flow": flow, "include_junk": True},
        notes=notes,
    )


@router.get("/flows/calls")
async def flow_calls(
    period: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    ended: str | None = Query(None, description="Filter to one `ended` reason, e.g. unrouted_hangup."),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    _: AuthedKey = Depends(require_scope(SCOPE_READ)),
) -> dict:
    """Individual calls with their full flow path — the drill-down behind `/flows/outcomes`.

    `ended=unrouted_hangup` is the useful filter: it lists the specific callers who were
    dropped, with the node path that got them there, so a flow fix can be verified against
    real calls rather than aggregates.
    """
    start, end, described = resolve_window(period, date_from, date_to)
    where = [
        CallEvent.event_type == SUMMARY_EVENT,
        REAL_CALL, Call.started_at >= start, Call.started_at < end,
    ]
    if ended:
        where.append(_f("ended") == ended)

    rows = (await db.execute(
        select(
            Call.provider_call_sid,
            Call.started_at,
            Call.duration_seconds,
            func.coalesce(Flow.name, "(no flow pinned)"),
            _f("ended"),
            _f("terminal_node"),
            _f("ms"),
            # Selected as jsonb (not cast to text) so the path comes back as a real JSON array
            # the caller can iterate, rather than a string they would have to re-parse.
            CallEvent.payload["flow"]["path"],
        )
        .select_from(CallEvent)
        .join(Call, Call.id == CallEvent.call_id)
        .outerjoin(FlowVersion, FlowVersion.id == Call.flow_version_id)
        .outerjoin(Flow, Flow.id == FlowVersion.flow_id)
        .where(*where)
        .order_by(Call.started_at.desc())
        .limit(limit)
    )).all()

    return ok(
        summary=f"{len(rows)} flow-run calls {periods.describe_window(described)}"
                + (f" that ended '{ended}'" if ended else "") + ".",
        data=[
            {
                "call_sid": sid,
                "started_at": at.isoformat() if at else None,
                "duration_seconds": dur,
                "flow": fname,
                "ended": e,
                "terminal_node": term,
                "flow_ms": int(ms) if ms and str(ms).isdigit() else None,
                "path": path,
            }
            for sid, at, dur, fname, e, term, ms, path in rows
        ],
        applied_filters={**described, "ended": ended, "limit": limit},
        notes=[NOTE_NOT_RETROACTIVE, NOTE_DROPPED],
    )

"""Listening to and seizing a live AI-agent call — the ONE place those jobs are queued.

Two doors lead here: an OWEN admin in OWEN's own UI (`api/telephony.py` `/monitor/*`), and
a CRM user through the CRM's backend (`integrations/crm/api.py` `/live-calls/*`). They
differ only in who the operator is and how that was established. What happens to the call
must not differ, so both call these functions rather than each building a `monitor_listen`
payload: a second copy is where "takeover forgot the agent's media channel" would come
from, and that bug leaves the agent talking over the human who just took the call.

Nothing here decides WHETHER the caller may do it. The routes do that (an admin role, or
the CRM key plus a provisioned operator) before they get here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import queue

NO_SESSION = "no live agent session for that linkedid"


class NoLiveSession(LookupError):
    """The call has ended, or was never an agent call. Routes answer 404 with NO_SESSION."""


async def _session(linkedid: str) -> dict | None:
    """The live agent session for a call: owen-voice's, else a Retell call's.

    A Retell call (RETELL-PLAN C7) has no owen-voice session — its conversation is on Retell's
    side of a SIP leg OWEN dialled — so the same three facts come from the Retell registry
    instead: the caller's channel, the Retell leg (ejected on take-over exactly as owen-voice's
    media channel is), and the bridge the operator joins. One shape, so `queue_listen` /
    `queue_takeover` and the monitor job stay ONE mechanism for both engines."""
    from app.telephony import voice_client

    sess = await voice_client.session_for(linkedid)
    if sess is not None:
        return sess
    return await retell_session(linkedid)


async def retell_session(linkedid: str) -> dict | None:
    """A live Retell call in owen-voice's session shape, or None. Best-effort: a registry
    that cannot be read means "not live", never an exception into a supervisor's request."""
    try:
        from app.integrations.retell import registry

        row = await registry.current().live_for(linkedid)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("telephony.supervision").exception(
            "supervision: Retell registry lookup failed for %s", linkedid)
        return None
    if row is None:
        return None
    return {
        "linkedid": linkedid,
        "engine": "retell",
        "call_channel_id": row.get("call_channel_id"),
        "media_channel_id": row.get("retell_channel_id"),
        "bridge_id": row.get("bridge_id"),
    }


async def retell_sessions() -> list[dict]:
    """Every live Retell call as a session row for `describe` (C7): linkedid, how long it has
    run, and `engine: "retell"`. Best-effort; [] when the registry cannot be read."""
    try:
        from app.integrations.retell import registry

        rows = await registry.current().live()
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("telephony.supervision").exception(
            "supervision: listing live Retell calls failed")
        return []
    from datetime import timezone

    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        started = r.get("created_at")
        duration = None
        if isinstance(started, datetime):
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            duration = int((now - started).total_seconds())
        out.append({"linkedid": r.get("linkedid"), "duration_s": duration, "turns": None,
                    "engine": "retell"})
    return out


async def queue_listen(db: AsyncSession, *, operator_id: str, linkedid: str,
                       channel_id: str | None = None) -> dict:
    """Ring `operator_id` and bridge them to a snoop of the call — inaudible to both parties."""
    sess = await _session(linkedid)
    target = channel_id or (sess or {}).get("call_channel_id")
    if not target:
        raise NoLiveSession(NO_SESSION)
    operator_channel_id = uuid.uuid4().hex
    await queue.enqueue(db, "monitor_listen", {
        "operator_id": operator_id,
        "target_channel_id": target,
        "linkedid": linkedid,
        "operator_channel_id": operator_channel_id,
    })
    return {"ok": True, "operator_channel": operator_channel_id}


async def queue_takeover(db: AsyncSession, *, operator_id: str, linkedid: str,
                         channel_id: str | None = None,
                         operator_channel_id: str | None = None,
                         snoop_channel_id: str | None = None,
                         monitor_bridge_id: str | None = None) -> dict:
    """Stop the agent and bridge `operator_id` to the caller, permanently.

    The agent's media channel and the call bridge come from owen-voice's session, never from
    the request: the caller of this cannot know them, and guessing wrong leaves the agent on
    the line."""
    sess = await _session(linkedid)
    target = channel_id or (sess or {}).get("call_channel_id")
    if not target:
        raise NoLiveSession(NO_SESSION)
    operator_channel_id = operator_channel_id or uuid.uuid4().hex
    await queue.enqueue(db, "monitor_takeover", {
        "operator_id": operator_id,
        "linkedid": linkedid,
        "target_channel_id": target,
        "operator_channel_id": operator_channel_id,
        "call_bridge_id": (sess or {}).get("bridge_id"),
        "snoop_channel_id": snoop_channel_id,
        "monitor_bridge_id": monitor_bridge_id,
        "agent_channel_id": (sess or {}).get("media_channel_id"),
    })
    return {"ok": True, "operator_channel": operator_channel_id, "owner": operator_id}


# --- the CRM's view of what is live ---------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def describe(sessions: list[dict], facts: dict[str, dict]) -> list[dict]:
    """The live-call rows the CRM draws. PURE.

    `sessions` is owen-voice's list (it knows the call is live and how long it has run);
    `facts` is OWEN's `calls` row for each linkedid (it knows who rang which number, and
    which agent answered). A session with no call row yet is still listed — the row is
    written at ingest and can trail the session by a moment — with the unknowns as None
    rather than dropped, because a live call the CRM cannot see is a call nobody can take
    over.

    Channel, bridge and session ids are NOT passed on. The CRM never needs them (takeover
    looks them up here, server-side) and a channel id is a handle on a live call.
    """
    out = []
    for s in sessions:
        linkedid = str(s.get("linkedid") or "")
        if not linkedid:
            continue
        f = facts.get(linkedid) or {}
        row = {
            "linkedid": linkedid,
            "caller_number": f.get("caller_number"),
            "dialed_number": f.get("dialed_number"),
            "agent": f.get("agent"),
            "started_at": _iso(f.get("started_at")),
            "duration_s": s.get("duration_s"),
            "turns": s.get("turns"),
        }
        # C7: a Retell call says so. owen_voice rows keep their exact documented shape — a
        # row with no `engine` IS an owen_voice call, as it always was.
        if s.get("engine") == "retell":
            row["engine"] = "retell"
        out.append(row)
    return out


async def call_facts(db: AsyncSession, linkedids: list[str]) -> dict[str, dict]:
    """`calls` rows for these linkedids (== `provider_call_sid` on an Asterisk call), with
    the caller's number, the dialed DID and the agent's name. One query, outer joins: a
    call with no caller row or no pinned agent version is still returned."""
    if not linkedids:
        return {}
    from app.models import Agent, AgentVersion, Call, Caller, Number

    rows = (
        await db.execute(
            select(Call.provider_call_sid, Call.started_at, Caller.phone_number,
                   Number.phone_number, Agent.name)
            .select_from(Call)
            .outerjoin(Caller, Caller.id == Call.caller_id)
            .outerjoin(Number, Number.id == Call.number_id)
            .outerjoin(AgentVersion, AgentVersion.id == Call.agent_version_id)
            .outerjoin(Agent, Agent.id == AgentVersion.agent_id)
            .where(Call.provider_call_sid.in_(linkedids))
        )
    ).all()
    out: dict[str, dict] = {}
    for sid, started_at, caller, dialed, agent in rows:
        out.setdefault(str(sid), {"started_at": started_at, "caller_number": caller,
                                  "dialed_number": dialed, "agent": agent})
    return out

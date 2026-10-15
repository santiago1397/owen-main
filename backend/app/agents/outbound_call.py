"""An AI agent places ONE outbound call because a PERSON asked it to (owner, 2026-10-15).

Built for the owner's first test ("call the boss") and left SWITCHED OFF: nothing happens
unless `AGENT_OUTBOUND_ENABLED=true`. Two doors, one path:

    POST /api/crm-link/agent-calls          the CRM key (integrations/crm/api.py)
    python -m app.agents.outbound_call ...  --yes, from a shell on the box (main() below)

Both call `place()`: every guard, then the attempt is written down (the daily cap and the
10-minute rule count it) and ONE `agent_outbound_call` job is queued. The worker runs it
(`run_call`), because only the worker's ARI consumer sees the leg answer.

## What the call does

    1. ring `to_number` over the BulkVS trunk, caller ID = our DID, ring
       AGENT_OUTBOUND_RING_SECONDS (30). The leg is the call's ENTRY leg — its own id is the
       Linkedid — marked `agent-outbound` so the consumer never runs a flow on it, and carrying
       X_OWEN_DIRECTION / X_OWEN_FROM / X_OWEN_TO so it is filed as an outbound call;
    2. no answer / busy / failed -> the CRM is told an OUTBOUND call with that status. No
       Retell registration (no cost). Nothing else; NEVER retried;
    3. answered -> `flows/runtime.run_agent_on_call(outbound=...)`: the very function an
       inbound `ai_agent` node runs — the Retell engine registers it with direction
       "outbound", the usual caller brief for the number called plus `call_direction`,
       `call_reason` and the outbound greeting (brief.for_outbound), dials Retell, bridges,
       records the bridge, waits; functions, transfer, take-over, Listen and the CRM's
       outbound call row (same `ai_call` shape) all come from that path;
    4. the person's leg is hung up when it ends, unless a supervisor owns the call now.

## The guards (each refuses with a sentence; nothing dialled, nothing sent to Retell)

AGENT_OUTBOUND_ENABLED; telephony on; RETELL_API_KEY set; a reason (<= 300); `to_number` a
full US number and NOT one of our own DIDs, and not blocked; the agent exists, has an active
version on engine "retell" with a retell_agent_id; `from_number` — given, else the ONE number
the CRM assigned this agent to, else AGENT_OUTBOUND_FROM, never invented — is an active
BulkVS number we own; 08:00-20:00 America/New_York (TCPA calling hours); at most
AGENT_OUTBOUND_DAILY_MAX a New York day; not the same number twice in
AGENT_OUTBOUND_REPEAT_MINUTES; the daily AI spend cap not reached.

## TCPA, said plainly

An AI voice is an "artificial voice" (FCC, February 2024): a call to a mobile with one needs
the called party's prior express consent (prior express WRITTEN consent if it is
telemarketing). This exists for calls a person decides to make to someone who expects it —
the boss, a customer who asked to be called back. Nothing in the system calls it
automatically: no trigger, no flow node, no job that loops over contacts. The agent says it
is an AI and that the call is recorded in its first sentence (brief.OUTBOUND_INTRO + NOTICE).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("agents.outbound_call")

JOB_TYPE = "agent_outbound_call"
# The attempts, kept for the daily cap and the 10-minute rule: `app_settings` row
# {"calls": [{"at", "to", "from", "agent", "linkedid", "by"}]}, pruned to two days.
LOG_KEY = "agent_outbound_calls"
LOG_KEEP = timedelta(days=2)

TZ = "America/New_York"
OPEN_HOUR, CLOSE_HOUR = 8, 20
HOURS_LABEL = f"{OPEN_HOUR:02d}:00-{CLOSE_HOUR:02d}:00 {TZ}"
MAX_REASON = 300

OFF = "Outbound AI calls are switched off on the phone system (AGENT_OUTBOUND_ENABLED)"

_US = re.compile(r"^\+1[2-9]\d{2}[2-9]\d{6}$")


class Refused(Exception):
    """Why the call was not placed. `status` is the HTTP answer the route gives."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class CallRequest:
    to_number: str
    agent_name: str
    reason: str
    from_number: str = ""
    customer_first_name: str = ""
    requested_by: str = ""


# --- pure kernels ---------------------------------------------------------------------------


def us_e164(number) -> str:
    """`number` as +1NXXNXXXXXX, or "" when it is not a full US number. PURE."""
    raw = str(number or "").strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    candidate = "+" + digits if digits else ""
    return candidate if _US.match(candidate) else ""


def within_hours(now: datetime) -> bool:
    """08:00 <= New York local time < 20:00. PURE (given `now`, offset-aware)."""
    local = now.astimezone(ZoneInfo(TZ))
    return OPEN_HOUR <= local.hour < CLOSE_HOUR


def _parse(at) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(at))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def used_today(entries: list, now: datetime) -> int:
    """How many calls were placed on `now`'s New York calendar day. PURE."""
    tz = ZoneInfo(TZ)
    today = now.astimezone(tz).date()
    return sum(1 for e in entries or []
               if (_parse((e or {}).get("at")) or datetime.min.replace(tzinfo=timezone.utc))
               .astimezone(tz).date() == today)


def called_recently(entries: list, to_number: str, now: datetime, minutes: int) -> bool:
    """Was `to_number` called less than `minutes` ago? PURE."""
    since = now - timedelta(minutes=max(0, int(minutes)))
    for e in entries or []:
        at = _parse((e or {}).get("at"))
        if (e or {}).get("to") == to_number and at is not None and at > since:
            return True
    return False


# --- the database, behind one small seam (tests use a memory store) -------------------------


class DbStore:
    """Every database touch `place()` makes. One session; `record()` commits the attempt and
    the job together, so a call is never queued without being counted."""

    def __init__(self, db):
        self.db = db

    async def agent(self, name: str) -> dict:
        from app.integrations.crm import agent_versions
        from app.models import AgentVersion

        try:
            agent = await agent_versions.the_agent(self.db, name)
        except agent_versions.Refused as r:
            raise Refused(r.status, r.message) from None
        version = (await self.db.get(AgentVersion, agent.active_version_id)
                   if agent.active_version_id else None)
        cfg = version.config if version is not None and isinstance(version.config, dict) else {}
        return {"id": str(agent.id), "name": agent.name,
                "active": version is not None,
                "version": getattr(version, "version", None),
                "engine": str(cfg.get("engine") or ""),
                "retell_agent_id": str(cfg.get("retell_agent_id") or "")}

    async def number(self, e164: str) -> Optional[dict]:
        from sqlalchemy import select

        from app.models import Number

        row = (await self.db.execute(
            select(Number).where(Number.phone_number == e164).limit(1))).scalar_one_or_none()
        if row is None:
            return None
        return {"phone_number": row.phone_number, "media_provider": row.media_provider,
                "active": row.active, "released_at": row.released_at,
                "provider_status": row.provider_status}

    async def assigned_numbers(self, agent_id: str, agent_name: str) -> list[str]:
        """The numbers the CRM gave this agent (C5 memory) that still run its flow."""
        from app.integrations.crm import numbers as crm_numbers

        out = []
        for number in await crm_numbers._all_numbers(self.db):
            memory = await crm_numbers._memory(self.db, number.id)
            if not memory or number.flow_id is None \
                    or str(number.flow_id) != str(memory.get("flow_id")):
                continue
            if str(memory.get("agent_id") or "") == agent_id \
                    or str(memory.get("agent_name") or "") == agent_name:
                out.append(number.phone_number)
        return out

    async def blocked(self, e164: str) -> bool:
        from app.services.messages import is_contact_blocked

        return bool(await is_contact_blocked(self.db, e164))

    async def log(self, *, lock: bool = False) -> list[dict]:
        from app.models import AppSetting

        row = await self.db.get(AppSetting, LOG_KEY, with_for_update=lock or None)
        value = row.value if row is not None and isinstance(row.value, dict) else {}
        return [e for e in (value.get("calls") or []) if isinstance(e, dict)]

    async def record(self, entries: list[dict], payload: dict) -> None:
        from app.models import AppSetting
        from app.services import queue

        row = await self.db.get(AppSetting, LOG_KEY)
        if row is None:
            self.db.add(AppSetting(key=LOG_KEY, value={"calls": entries}))
        else:
            row.value = {"calls": entries}
        await queue.enqueue(self.db, JOB_TYPE, payload)   # commits both


async def _over_cap() -> bool:
    from app.agents import spend

    return await spend.over_cap()


# --- the door -------------------------------------------------------------------------------


async def status(store) -> dict:
    """What the CRM shows: is it on, from which number by default, how many are left."""
    from app.core.config import settings

    try:
        used = used_today(await store.log(), datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 - a status read never fails the page
        logger.exception("agent outbound: reading the call log failed")
        used = None
    return {"enabled": bool(settings.AGENT_OUTBOUND_ENABLED),
            "from_number": (settings.AGENT_OUTBOUND_FROM or "").strip() or None,
            "daily_max": int(settings.AGENT_OUTBOUND_DAILY_MAX or 0),
            "used_today": used,
            "hours": HOURS_LABEL}


def _off_refusal() -> Optional[Refused]:
    from app.core.config import settings

    if not settings.AGENT_OUTBOUND_ENABLED:
        return Refused(503, OFF)
    return None


async def _from_number(store, req: CallRequest, agent: dict) -> str:
    from app.core.config import settings

    given = (req.from_number or "").strip()
    if given:
        number = us_e164(given)
        if not number:
            raise Refused(422, "from_number must be a full US number in E.164 (+1XXXXXXXXXX)")
        return number
    assigned = sorted(set(await store.assigned_numbers(agent["id"], agent["name"])))
    if len(assigned) == 1:
        return assigned[0]
    fallback = (settings.AGENT_OUTBOUND_FROM or "").strip()
    if fallback:
        number = us_e164(fallback)
        if not number:
            raise Refused(409, "AGENT_OUTBOUND_FROM is not a full US number in E.164")
        return number
    if assigned:
        raise Refused(422, f"the agent '{agent['name']}' answers {len(assigned)} numbers; "
                           "say which one to call from (from_number)")
    raise Refused(422, f"the agent '{agent['name']}' is not assigned to a number and "
                       "AGENT_OUTBOUND_FROM is not set; say which number to call from "
                       "(from_number)")


async def place(store, req: CallRequest, *, now: Optional[datetime] = None) -> dict:
    """Every guard, then count the attempt and queue the ONE job. Raises `Refused`."""
    from app.core.config import settings

    off = _off_refusal()
    if off is not None:
        raise off
    if not settings.ASTERISK_ENABLED:
        raise Refused(503, "telephony is not enabled on the phone system (ASTERISK_ENABLED)")
    if not (settings.RETELL_API_KEY or "").strip():
        raise Refused(503, "Retell is not configured on the phone system (RETELL_API_KEY)")

    reason = " ".join(str(req.reason or "").split())
    if not reason:
        raise Refused(422, "say what the call is about (reason)")
    if len(reason) > MAX_REASON:
        raise Refused(422, f"the reason is {len(reason)} characters; at most {MAX_REASON}")
    to = us_e164(req.to_number)
    if not to:
        raise Refused(422, "to_number must be a full US number in E.164 (+1XXXXXXXXXX)")
    own = await store.number(to)
    if own is not None and own.get("released_at") is None:
        raise Refused(422, "that is one of our own numbers; an agent does not call itself")

    name = (req.agent_name or "").strip()
    if not name:
        raise Refused(422, "say which agent makes the call (agent_name)")
    agent = await store.agent(name)
    if not agent.get("active"):
        raise Refused(409, f"the agent '{agent['name']}' has no active version")
    if agent.get("engine") != "retell" or not agent.get("retell_agent_id"):
        raise Refused(409, f"the agent '{agent['name']}' is not a Retell agent; only Retell "
                           "agents place calls")

    from_number = await _from_number(store, req, agent)
    from app.integrations.crm.numbers import assignable_reason

    row = await store.number(from_number)
    why = ("it is not one of our numbers" if row is None else assignable_reason(
        media_provider=row.get("media_provider"), expected_media=settings.BULKVS_MEDIA_PROVIDER,
        active=row.get("active"), released_at=row.get("released_at"),
        provider_status=row.get("provider_status")))
    if why:
        raise Refused(409, f"cannot call from {from_number}: {why}")
    if await store.blocked(to):
        raise Refused(409, "this number is blocked in OWEN")

    now = now or datetime.now(timezone.utc)
    if not within_hours(now):
        raise Refused(409, f"agents only call between {HOURS_LABEL} (calling hours); it is "
                           f"{now.astimezone(ZoneInfo(TZ)):%H:%M} there now")

    entries = await store.log(lock=True)
    daily_max = int(settings.AGENT_OUTBOUND_DAILY_MAX or 0)
    if used_today(entries, now) >= daily_max:
        raise Refused(429, f"the agents have placed {daily_max} calls today, the most "
                           "allowed a day (AGENT_OUTBOUND_DAILY_MAX)")
    minutes = int(settings.AGENT_OUTBOUND_REPEAT_MINUTES or 0)
    if called_recently(entries, to, now, minutes):
        raise Refused(429, f"an agent called this number less than {minutes} minutes ago")
    if await _over_cap():
        raise Refused(409, "the daily AI spend cap is reached")

    from app.providers.asterisk import AGENT_OUTBOUND_CHANNEL_PREFIX

    linkedid = f"{AGENT_OUTBOUND_CHANNEL_PREFIX}{uuid.uuid4().hex}"
    first = " ".join(str(req.customer_first_name or "").split())[:40]
    payload = {"linkedid": linkedid, "to_number": to, "from_number": from_number,
               "agent_id": agent["id"], "agent_name": agent["name"],
               "version": agent.get("version"), "reason": reason, "first_name": first}
    keep_after = now - LOG_KEEP
    kept = [e for e in entries if (_parse(e.get("at")) or now) > keep_after]
    kept.append({"at": now.isoformat(), "to": to, "from": from_number, "agent": agent["name"],
                 "linkedid": linkedid, "by": (req.requested_by or "")[:120]})
    await store.record(kept, payload)
    logger.info("agent outbound: queued %s -> %s by agent %r (linkedid=%s, by %s)",
                from_number, to, agent["name"], linkedid, req.requested_by or "?")
    return {"ok": True, "call_id": linkedid, "linkedid": linkedid, "status": "dialing",
            "from_number": from_number, "to_number": to, "agent_name": agent["name"]}


# --- the worker half ------------------------------------------------------------------------


async def _provider_id() -> int:
    from app.db import SessionLocal
    from app.services.ingestion import _get_or_create_provider

    async with SessionLocal() as db:
        provider = await _get_or_create_provider(db, "asterisk")
        await db.commit()
        return provider.id


async def _report_unanswered(payload: dict, port: str) -> None:
    """The CRM's outbound call row for a call nobody answered: the status, who would have
    spoken and why. No Retell call exists, so there is nothing else to say."""
    from app.agents.crm_call import ai_call_record, dedupe_key
    from app.db import SessionLocal
    from app.integrations.crm import push as crm_push
    from app.integrations.crm.events import CallEventFacts

    lid = str(payload.get("linkedid") or "")
    owen_call_id = None
    try:
        async with SessionLocal() as db:
            owen_call_id, _rec, _trans = await crm_push.call_artifacts(db, lid)
    except Exception:  # noqa: BLE001 - the report goes out keyed on the linkedid instead
        logger.exception("agent outbound: call lookup failed (linkedid=%s)", lid)
    record = ai_call_record(agent_name=str(payload.get("agent_name") or ""),
                            version=payload.get("version"), outcome=port, captured=None)
    record.update({"direction": "outbound", "reason": str(payload.get("reason") or "")})
    await crm_push.enqueue_call_event(CallEventFacts(
        phase="ended",
        owen_call_id=owen_call_id or "",
        linkedid=lid,
        caller_number=str(payload.get("to_number") or ""),
        dialed_number=str(payload.get("from_number") or ""),
        direction="outbound",
        outcome=port if port in ("noanswer", "busy") else "failed",
        duration_seconds=0,
        extra={"ai_call": record, "dedupe_key": dedupe_key(owen_call_id or lid)},
    ))


async def _quiet(coro) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001 - teardown is best-effort
        logger.debug("agent outbound: teardown step failed", exc_info=True)


async def run_call(ari, payload: dict) -> str:
    """Ring the person; on answer run the agent on their channel. Returns the outcome (the
    dial port, or the agent's port). Never raises, never retries."""
    from app.core.calllog import clog
    from app.core.config import settings
    from app.flows import dtmf
    from app.flows import runtime
    from app.telephony import ownership

    lid = str(payload.get("linkedid") or "")
    to, from_number = str(payload.get("to_number") or ""), str(payload.get("from_number") or "")
    queue = dtmf.watch(lid)
    outcome = "failed"
    try:
        clog(logger, "agent_outbound.dial", linkedid=lid, to=to, from_number=from_number,
             agent=payload.get("agent_name"))
        port, info = await ari.originate_agent_outbound(
            queue, lid, to, caller_id=from_number,
            timeout_s=float(settings.AGENT_OUTBOUND_RING_SECONDS or 30),
            variables={"X_OWEN_DIRECTION": "outbound", "X_OWEN_FROM": from_number,
                       "X_OWEN_TO": to},
        )
        clog(logger, "agent_outbound.dial_result", linkedid=lid, result=port,
             cause=(info or {}).get("dial_end_cause"))
        dtmf.unwatch(queue, lid)
        queue = None
        if port != "answered":
            outcome = port
            try:
                await _report_unanswered(payload, port)
            except Exception:  # noqa: BLE001 - the CRM never affects the call
                logger.exception("agent outbound: reporting %s failed (linkedid=%s)", port, lid)
            return outcome
        run = await runtime.run_agent_on_call(
            ari=ari, channel_id=lid, lid=lid, dialed=from_number, caller_number=to,
            provider_id=await _provider_id(), agent_id=str(payload.get("agent_id") or ""),
            outbound={"reason": payload.get("reason") or "",
                      "first_name": payload.get("first_name") or ""},
        )
        outcome = run.port
        return outcome
    except Exception:  # noqa: BLE001 - a detached task: log, never raise
        logger.exception("agent outbound: the call failed (linkedid=%s)", lid)
        return outcome
    finally:
        if queue is not None:
            dtmf.unwatch(queue, lid)
        # The person's leg ends with the agent's part — unless a supervisor took the call over,
        # and then it is theirs (telephony/ownership.py). A leg already gone is a harmless 404.
        if not ownership.is_owned(lid):
            await _quiet(ari.hangup(lid))
        clog(logger, "agent_outbound.end", linkedid=lid, outcome=outcome)


# --- the CLI: how the first test call is started -------------------------------------------


def _args(argv=None):
    p = argparse.ArgumentParser(
        prog="python -m app.agents.outbound_call",
        description="Have an AI agent call ONE number now, because you asked it to. Needs "
                    "AGENT_OUTBOUND_ENABLED=true; refuses without --yes.")
    p.add_argument("--to", required=True, help="the number to call, E.164 (+1XXXXXXXXXX)")
    p.add_argument("--agent", required=True, help="the agent's name, as in the CRM")
    p.add_argument("--reason", required=True, help="what the call is about (<= 300 chars)")
    p.add_argument("--from", dest="from_number", default="",
                   help="our DID to call from (default: the agent's number, else "
                        "AGENT_OUTBOUND_FROM)")
    p.add_argument("--first-name", default="", help="the person's first name, if known")
    p.add_argument("--yes", action="store_true", help="really place the call")
    return p.parse_args(argv)


async def _main(args) -> int:
    from app.db import SessionLocal

    req = CallRequest(to_number=args.to, agent_name=args.agent, reason=args.reason,
                      from_number=args.from_number, customer_first_name=args.first_name,
                      requested_by="cli")
    try:
        async with SessionLocal() as db:
            out = await place(DbStore(db), req)
    except Refused as r:
        print(f"REFUSED ({r.status}): {r.message}")
        return 1
    print(f"Queued: {out['agent_name']} calls {out['to_number']} from {out['from_number']} "
          f"(linkedid {out['linkedid']}). The worker dials it now.")
    return 0


def main(argv=None) -> int:
    args = _args(argv)
    off = _off_refusal()
    if off is not None:
        print(f"REFUSED: {off.message}")
        return 1
    if not args.yes:
        print("REFUSED: this places a REAL phone call with an AI voice. Add --yes to place it. "
              "Nothing was dialled.")
        return 2
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())

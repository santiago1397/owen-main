"""Which number goes to which agent — assigned in the CRM, built here (RETELL-PLAN C5, decision 6).

The CRM (AI Agents -> Phone numbers, ADMIN) says "this number, this agent, this mode". OWEN
keeps the call flow, so it BUILDS the flow for that number from a template and remembers what
was there before, so taking the assignment away puts the old behaviour back exactly.

## The three templates

Every one plays the recording-consent notice first — an agent's call is recorded and Florida
is all-party consent, so a number whose consent notice is not configured is REFUSED (409)
rather than answered unannounced. Every one ends in voicemail when the agent cannot take the
call (`failed`: no active version, spend cap, Retell down) — decision 17.

    ai_first        consent -> agent -> (failed / transfer without a destination) voicemail
    staff_then_ai   consent -> ring the staff -> answered: done
                                              -> no answer / busy / failed: agent -> voicemail
    after_hours_ai  consent -> hours -> open:   ring the staff -> no answer: voicemail
                                     -> closed: agent -> voicemail

"The staff" is the CRM-line ring group's operators when the number has an enabled `crm_links`
binding naming them, else every operator on `CRM_LINK_SOFTPHONE_OPERATORS` — the people who
have a browser line. None at all -> 409: a template that rings nobody is a dead end.

KNOWN LIMIT, said rather than hidden: the CRM line's HYBRID ring also rings up to two mobile
numbers in parallel (`ring.py`); a flow `dial` node rings operators OR a number, not both at
once, so the template rings the operators only. And a number with a flow no longer takes the
CRM-bound default path at all (`handler.py`: an assigned flow wins), so assigning a CRM-bound
DID moves it off that path until the assignment is removed.

`after_hours_ai` needs `hours` — `{"tz", "days": {"mon": [["08:00","17:00"]], ...}}` — and is
refused without them: no invented default. (An `hours` node with no schedule fails OPEN, which
here would mean "the agent never answers", silently.)

## What is CRM-managed

The flow this module built for a number, recorded in `app_settings` under
`crm_number_assignment:<number id>` together with the flow it replaced. A number running
ANY other flow is running a hand-built one, and replacing it needs `replace: true` (409
otherwise) — an operator's flow is never overwritten by a CRM click.

Only numbers whose MEDIA rides on OWEN's Asterisk can be assigned: the runtime resolves flows
by (phone_number, media_provider), so a flow anywhere else could never run. The rest are
listed with the reason.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("integrations.crm.numbers")

MODES = ("ai_first", "staff_then_ai", "after_hours_ai")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SETTING_PREFIX = "crm_number_assignment:"
_DAY_NAMES = {**{d: d for d in DAYS}, "monday": "mon", "tuesday": "tue", "wednesday": "wed",
              "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun"}
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def setting_key(number_id) -> str:
    return f"{SETTING_PREFIX}{number_id}"


# --- pure kernels ---------------------------------------------------------------------------


def hours_problems(hours) -> tuple[Optional[dict], list[str]]:
    """C5 `hours` -> the `hours` node's own config `{"tz", "schedule"}`, or the problems.

    Strict on purpose: a typo here decides whether a customer reaches a person or a machine,
    so "8:00", "25:00" or an unknown day is refused with a sentence, never guessed at."""
    problems: list[str] = []
    if not isinstance(hours, dict):
        return None, ["hours must be {\"tz\": ..., \"days\": {\"mon\": [[\"08:00\", \"17:00\"]], ...}}"]
    tz = str(hours.get("tz") or "").strip()
    try:
        ZoneInfo(tz)
    except Exception:  # noqa: BLE001 - any failure means "not a timezone"
        problems.append(f"hours.tz {tz!r} is not a timezone (e.g. America/New_York)")
    days = hours.get("days")
    if not isinstance(days, dict) or not days:
        problems.append("hours.days must name at least one day")
        return None, problems
    schedule: dict = {}
    for day, windows in days.items():
        d = _DAY_NAMES.get(str(day).strip().lower())
        if d is None:
            problems.append(f"hours.days has an unknown day {day!r} (mon ... sun)")
            continue
        if not isinstance(windows, list):
            problems.append(f"hours.days.{d} must be a list of [\"HH:MM\", \"HH:MM\"]")
            continue
        out = []
        for w in windows:
            if not (isinstance(w, (list, tuple)) and len(w) == 2
                    and all(isinstance(x, str) and _HHMM.match(x) for x in w)):
                problems.append(f"hours.days.{d} has a window {w!r} that is not "
                                "[\"HH:MM\", \"HH:MM\"] in 24-hour time")
                continue
            if w[0] >= w[1]:
                problems.append(f"hours.days.{d} window {w[0]}-{w[1]} ends before it starts")
                continue
            out.append([w[0], w[1]])
        schedule[d] = out
    if problems:
        return None, problems
    return {"tz": tz, "schedule": schedule}, []


def build_graph(mode: str, *, agent_id: str, agent_name: str, consent: str, greeting: str,
                operators: list[str], ring_timeout: int, record: bool,
                hours: Optional[dict] = None) -> dict:
    """The flow graph for a mode. PURE; `flows.validate_graph` must accept it (a test pins
    every mode). Node ids are stable so two versions of one number's flow diff cleanly."""
    agent = {"type": "ai_agent", "agent_id": str(agent_id), "agent_name": agent_name,
             "next": {"default": "end", "complete": "end", "transfer": "voicemail",
                      "failed": "voicemail"}}
    nodes: dict = {
        "entry": {"type": "entry", "next": {"default": "consent"}},
        "consent": {"type": "play", "media": consent, "next": {"default": None}},
        "agent": agent,
        "voicemail": {"type": "voicemail", "greeting": greeting},
        "end": {"type": "hangup"},
    }

    def ring(no_answer: str) -> dict:
        return {"type": "dial", "target_kind": "operator", "operators": list(operators),
                "timeout": int(ring_timeout), "record": bool(record),
                "next": {"answered": "end", "noanswer": no_answer, "busy": no_answer,
                         "failed": no_answer}}

    if mode == "ai_first":
        nodes["consent"]["next"]["default"] = "agent"
    elif mode == "staff_then_ai":
        nodes["ring"] = ring("agent")
        nodes["consent"]["next"]["default"] = "ring"
    elif mode == "after_hours_ai":
        nodes["hours"] = {"type": "hours", "hours": dict(hours or {}),
                          "next": {"open": "ring", "closed": "agent"}}
        nodes["ring"] = ring("voicemail")
        nodes["consent"]["next"]["default"] = "hours"
    else:
        raise ValueError(f"unknown mode {mode!r}")
    return {"nodes": nodes, "default_fallback": "voicemail",
            "crm_managed": {"mode": mode, "agent_name": agent_name}}


def assignable_reason(*, media_provider, expected_media: str, active, released_at,
                      provider_status) -> Optional[str]:
    """Why a number cannot be assigned, or None. PURE."""
    from app.services.number_sync import is_carrier_active

    if released_at is not None:
        return "this number has been released"
    if (media_provider or "") != expected_media:
        return ("this number's calls do not run through OWEN's phone system, so no flow can "
                "answer it" + (f" (media on {media_provider})" if media_provider else ""))
    if active is False:
        return "this number is switched off"
    if not is_carrier_active(provider_status):
        return f"the carrier reports this number as {provider_status!r}, not Active"
    return None


# --- the async glue (each DB touch is a small named function, so tests can stand in) --------


async def _all_numbers(db) -> list:
    from sqlalchemy import select

    from app.models import Number

    return list((await db.execute(
        select(Number).where(Number.released_at.is_(None)).order_by(Number.phone_number)
    )).scalars().all())


async def _memory(db, number_id) -> Optional[dict]:
    from app.models import AppSetting

    row = await db.get(AppSetting, setting_key(number_id))
    return dict(row.value) if row is not None and isinstance(row.value, dict) else None


async def _remember(db, number_id, value: Optional[dict]) -> None:
    from app.models import AppSetting

    row = await db.get(AppSetting, setting_key(number_id))
    if value is None:
        if row is not None:
            await db.delete(row)
        return
    if row is None:
        db.add(AppSetting(key=setting_key(number_id), value=value))
    else:
        row.value = value


async def _agent(db, name: str):
    from app.integrations.crm import agent_versions

    try:
        return await agent_versions.the_agent(db, name)
    except agent_versions.Refused as r:
        raise Refused(r.status, r.message) from None


async def _versions(db, flow_id) -> list[int]:
    from sqlalchemy import select

    from app.models import FlowVersion

    return list((await db.execute(
        select(FlowVersion.version).where(FlowVersion.flow_id == flow_id)
    )).scalars().all())


async def _operators_for(db, number) -> list[str]:
    """The staff a template rings: the CRM binding's named operators, else the roster."""
    from app.core.config import settings
    from app.integrations.crm import binding as crm_binding
    from app.integrations.crm import softphone as crm_softphone

    try:
        bound = await crm_binding.resolve(db, number.phone_number)
    except Exception:  # noqa: BLE001 - a binding is a preference, not a requirement
        bound = None
    if bound is not None and bound.operator_ids:
        return list(bound.operator_ids)
    return sorted(crm_softphone.parse_roster(settings.CRM_LINK_SOFTPHONE_OPERATORS))


def _number_row(number, memory: Optional[dict], expected_media: str) -> dict:
    reason = assignable_reason(
        media_provider=number.media_provider, expected_media=expected_media,
        active=number.active, released_at=number.released_at,
        provider_status=number.provider_status)
    in_force = (memory is not None and number.flow_id is not None
                and str(number.flow_id) == str(memory.get("flow_id")))
    return {
        "id": str(number.id),
        "e164": number.phone_number,
        "label": number.friendly_name,
        "assignable": reason is None,
        "reason": reason,
        "assignment": ({"agent_name": memory.get("agent_name"), "mode": memory.get("mode"),
                        "hours": memory.get("hours")} if in_force else None),
    }


async def list_numbers(db) -> dict:
    from app.core.config import settings

    out = []
    for number in await _all_numbers(db):
        out.append(_number_row(number, await _memory(db, number.id),
                               settings.BULKVS_MEDIA_PROVIDER))
    return {"numbers": out}


async def _load_number(db, number_id):
    from app.models import Number

    try:
        nid = uuid.UUID(str(number_id))
    except ValueError:
        raise Refused(404, "no number with that id") from None
    number = await db.get(Number, nid)
    if number is None:
        raise Refused(404, "no number with that id")
    return number


async def assign(db, number_id, *, agent_name: str, mode: str, hours=None,
                 replace: bool = False) -> dict:
    """PUT: build (or re-version) the CRM-managed flow for this number and point it there."""
    from app.core.config import settings
    from app.flows.validator import validate_graph
    from app.models import Flow, FlowVersion

    number = await _load_number(db, number_id)
    reason = assignable_reason(
        media_provider=number.media_provider, expected_media=settings.BULKVS_MEDIA_PROVIDER,
        active=number.active, released_at=number.released_at,
        provider_status=number.provider_status)
    if reason:
        raise Refused(409, reason)
    if mode not in MODES:
        raise Refused(422, f"mode must be one of {', '.join(MODES)}")
    hours_cfg = None
    if mode == "after_hours_ai":
        if hours is None:
            raise Refused(422, "after_hours_ai needs hours — the open hours staff answer in; "
                               "there is no default")
        hours_cfg, problems = hours_problems(hours)
        if problems:
            raise Refused(422, "; ".join(problems))
    consent = (settings.INBOUND_CONSENT_MEDIA or "").strip()
    if not consent:
        raise Refused(409, "the recording-consent notice is not configured "
                           "(INBOUND_CONSENT_MEDIA); an agent's call is recorded, so no number "
                           "can be given to an agent without it")
    agent = await _agent(db, agent_name)
    operators: list[str] = []
    if mode in ("staff_then_ai", "after_hours_ai"):
        operators = await _operators_for(db, number)
        if not operators:
            raise Refused(409, "there is nobody to ring: no operators on this number's CRM "
                               "line and none on CRM_LINK_SOFTPHONE_OPERATORS")

    memory = await _memory(db, number.id) or {}
    managed_id = memory.get("flow_id")
    current = str(number.flow_id) if number.flow_id is not None else None
    hand_built = current is not None and current != str(managed_id or "")
    if hand_built and not replace:
        flow = await db.get(Flow, number.flow_id)
        name = getattr(flow, "name", None) or current
        raise Refused(409, f"this number runs the hand-built flow {name!r}; send "
                           "\"replace\": true to replace it (removing the assignment later "
                           "puts it back)")
    previous = current if hand_built else memory.get("previous_flow_id")

    graph = build_graph(
        mode, agent_id=str(agent.id), agent_name=agent.name, consent=consent,
        greeting=str(getattr(settings, "VOICEMAIL_GREETING", "") or ""), operators=operators,
        ring_timeout=int(getattr(settings, "OPERATOR_RING_TIMEOUT_SECONDS", 25) or 25),
        record=bool(getattr(settings, "INBOUND_RECORDING_ENABLED", True)), hours=hours_cfg)
    check = validate_graph(graph)
    if not check.ok:  # a template bug, never the CRM's fault — but never activate it
        logger.error("crm numbers: template %s failed validation: %s", mode, check.errors)
        raise Refused(500, "the phone system built a flow it cannot run; nothing was changed")

    flow = await db.get(Flow, uuid.UUID(str(managed_id))) if managed_id else None
    if flow is None or getattr(flow, "archived_at", None) is not None:
        flow = Flow(id=uuid.uuid4(), name=f"CRM: {number.phone_number}")
        db.add(flow)
        version_no = 1
    else:
        from app.flows.service import next_version_number

        version_no = next_version_number(await _versions(db, flow.id))
    flow.name = f"CRM: {number.phone_number} ({mode}, {agent.name})"
    fv = FlowVersion(id=uuid.uuid4(), flow_id=flow.id, version=version_no, graph=graph)
    db.add(fv)
    await db.flush()
    flow.active_version_id = fv.id
    number.flow_id = flow.id
    await _remember(db, number.id, {
        "agent_name": agent.name, "agent_id": str(agent.id), "mode": mode,
        "hours": hours if mode == "after_hours_ai" else None,
        "flow_id": str(flow.id), "previous_flow_id": previous,
        "assigned_at": datetime.now(timezone.utc).isoformat(),
    })
    await db.commit()
    logger.info("crm numbers: %s now runs %s with agent %r (flow %s v%s; previous %s)",
                number.phone_number, mode, agent.name, flow.id, version_no, previous)
    return {**_number_row(number, await _memory(db, number.id), settings.BULKVS_MEDIA_PROVIDER),
            "flow_id": str(flow.id), "previous_flow_id": previous}


async def unassign(db, number_id) -> dict:
    """DELETE: put back the flow the CRM replaced (or none), and retire the managed flow."""
    from app.core.config import settings
    from app.models import Flow

    number = await _load_number(db, number_id)
    memory = await _memory(db, number.id)
    if not memory:
        raise Refused(404, "this number has no CRM assignment to remove")
    managed_id = str(memory.get("flow_id") or "")
    note = None
    if number.flow_id is not None and str(number.flow_id) == managed_id:
        previous = memory.get("previous_flow_id")
        restored = None
        if previous:
            flow = await db.get(Flow, uuid.UUID(str(previous)))
            if flow is not None and flow.archived_at is None and flow.active_version_id:
                restored = flow.id
            else:
                note = ("the flow this number ran before is gone or has no active version, so "
                        "it now takes the default call handling")
        number.flow_id = restored
    else:
        # Someone changed the number's flow by hand since; that is theirs, and stays.
        note = "the number's flow was changed by hand since; it was left as it is"
    if managed_id:
        managed = await db.get(Flow, uuid.UUID(managed_id))
        if managed is not None and managed.archived_at is None:
            managed.archived_at = datetime.now(timezone.utc)
    await _remember(db, number.id, None)
    await db.commit()
    logger.info("crm numbers: assignment removed from %s; flow now %s",
                number.phone_number, number.flow_id)
    row = _number_row(number, None, settings.BULKVS_MEDIA_PROVIDER)
    return {**row, "flow_id": str(number.flow_id) if number.flow_id else None, "note": note}

"""Quo-style per-contact Inbox API (/inbox page): one thread per CONTACT across every
BulkVS DID, messages AND calls folded into a single timeline.

Scope is the PLATFORM provider bucket only (bulkvs/asterisk) — the legacy /api/messages
surface (per-(number, caller) threads, all providers) stays untouched.

State model (see services/inbox_threads.py):
  - read/open state lives per contact in contact_thread_state, written ONLY here by user
    actions; unread + auto-reopen are derived, so webhooks never write state.
  - outbound from-number: sticky per contact (the DID of the contact's last interaction),
    falling back to the global default DID (app_settings 'inbox_default_number_id');
    SMS additionally requires the chosen DID to pass the 10DLC gate.
"""

import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.db import get_db
from app.models import (
    AppSetting,
    Call,
    Caller,
    ContactNote,
    ContactThreadState,
    Message,
    Number,
    Provider,
    Recording,
    User,
)
from app.services import queue, sms
from app.services.inbox_threads import (
    DidRef,
    merge_threads,
    resolve_call_from,
    resolve_sms_from,
)
from app.services.messages import enqueue_outbound_message, get_optout_state
from app.services.number_sync import is_carrier_active

router = APIRouter(prefix="/api/inbox", tags=["inbox"])

PLATFORM_PROVIDERS = ("bulkvs", "asterisk")
DEFAULT_NUMBER_KEY = "inbox_default_number_id"

# Same in-process derivation cap the legacy Messages inbox uses.
_SCAN_LIMIT = 2000


# --- shared row shapes -------------------------------------------------------------------


def _msg_stmt():
    return (
        select(
            Message.id,
            Message.caller_id,
            Message.number_id,
            Message.direction,
            Message.body,
            Message.status,
            Message.num_media,
            Message.media_urls,
            Message.received_at,
            Caller.phone_number.label("caller_number"),
            Number.phone_number.label("number_phone"),
            Number.sms_enabled.label("sms_enabled"),
            Number.sms_campaign_id.label("sms_campaign_id"),
        )
        .join(Provider, Message.provider_id == Provider.id)
        .join(Caller, Message.caller_id == Caller.id, isouter=True)
        .join(Number, Message.number_id == Number.id, isouter=True)
        .where(Provider.name.in_(PLATFORM_PROVIDERS))
    )


def _call_stmt():
    return (
        select(
            Call.id,
            Call.caller_id,
            Call.number_id,
            Call.direction,
            Call.status,
            Call.started_at,
            Call.duration_seconds,
            Caller.phone_number.label("caller_number"),
            Number.phone_number.label("number_phone"),
            Number.sms_enabled.label("sms_enabled"),
            Number.sms_campaign_id.label("sms_campaign_id"),
        )
        .join(Provider, Call.provider_id == Provider.id)
        .join(Caller, Call.caller_id == Caller.id, isouter=True)
        .join(Number, Call.number_id == Number.id, isouter=True)
        .where(Provider.name.in_(PLATFORM_PROVIDERS))
    )


class _Row:
    """Attribute view over a mappings() row for the pure merge helpers."""

    def __init__(self, m):
        for k, v in m.items():
            setattr(self, k, v)


async def _default_did(db: AsyncSession) -> DidRef | None:
    row = await db.get(AppSetting, DEFAULT_NUMBER_KEY)
    nid = (row.value or {}).get("number_id") if row else None
    if not nid:
        return None
    number = await db.get(Number, uuid.UUID(nid))
    if number is None or not number.active or not is_carrier_active(number.provider_status):
        return None
    return DidRef(
        number_id=str(number.id),
        phone_number=number.phone_number,
        sms_enabled=number.sms_enabled,
        sms_campaign_id=number.sms_campaign_id,
    )


def _did_out(d: DidRef | None) -> dict | None:
    return (
        {"number_id": d.number_id, "phone_number": d.phone_number}
        if d and d.number_id
        else None
    )


# --- threads -----------------------------------------------------------------------------


@router.get("/threads")
async def list_threads(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    msg_rows = (
        (await db.execute(_msg_stmt().order_by(Message.received_at.desc()).limit(_SCAN_LIMIT)))
        .mappings()
        .all()
    )
    call_rows = (
        (await db.execute(_call_stmt().order_by(Call.started_at.desc()).limit(_SCAN_LIMIT)))
        .mappings()
        .all()
    )
    states = {
        str(s.caller_id): (s.last_read_at, s.closed_at)
        for s in (await db.execute(select(ContactThreadState))).scalars().all()
    }
    threads = merge_threads([_Row(r) for r in msg_rows], [_Row(r) for r in call_rows], states)

    caller_ids = [uuid.UUID(t.caller_id) for t in threads]
    callers = {
        str(c.id): c
        for c in (
            (await db.execute(select(Caller).where(Caller.id.in_(caller_ids)))).scalars().all()
            if caller_ids
            else []
        )
    }
    default = await _default_did(db)

    out = []
    for t in threads:
        c = callers.get(t.caller_id)
        sms_from, sms_fallback, sms_reason = resolve_sms_from(t.sticky, default)
        call_from = resolve_call_from(t.sticky, default)
        out.append(
            {
                "caller_id": t.caller_id,
                "contact_number": t.caller_number,
                "contact_name": c.label if c else None,
                "company": c.company if c else None,
                "role": c.role if c else None,
                "last_at": t.last_at,
                "last_kind": t.last_kind,
                "last_direction": t.last_direction,
                "last_preview": t.last_preview,
                "message_count": t.message_count,
                "call_count": t.call_count,
                "unread_count": t.unread_count,
                "open": t.open,
                "responded": t.responded,
                "sticky_number": _did_out(t.sticky),
                "call_from": _did_out(call_from),
                "sms_from": _did_out(sms_from),
                "sms_via_fallback": sms_fallback,
                "sms_disabled_reason": sms_reason,
            }
        )
    return out


# --- one contact's timeline --------------------------------------------------------------


@router.get("/thread/{caller_id}")
async def get_thread(
    caller_id: uuid.UUID,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    caller = await db.get(Caller, caller_id)
    if caller is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")

    msg_rows = (
        (await db.execute(_msg_stmt().where(Message.caller_id == caller_id))).mappings().all()
    )
    call_rows = (
        (await db.execute(_call_stmt().where(Call.caller_id == caller_id))).mappings().all()
    )

    # First recording per call (for inline playback via /api/recordings/{id}/play).
    call_ids = [r["id"] for r in call_rows]
    recordings: dict = {}
    if call_ids:
        for rec in (
            (
                await db.execute(
                    select(Recording)
                    .where(Recording.call_id.in_(call_ids))
                    .order_by(Recording.downloaded_at.desc().nullslast())
                )
            )
            .scalars()
            .all()
        ):
            recordings.setdefault(rec.call_id, str(rec.id))

    items = [
        {
            "type": "message",
            "id": str(r["id"]),
            "direction": r["direction"],
            "body": r["body"],
            "status": r["status"],
            "num_media": r["num_media"],
            "media_urls": r["media_urls"] or [],
            "at": r["received_at"],
            "our_number": r["number_phone"],
        }
        for r in msg_rows
    ] + [
        {
            "type": "call",
            "id": str(r["id"]),
            "direction": r["direction"],
            "status": r["status"],
            "duration_seconds": r["duration_seconds"],
            "at": r["started_at"],
            "our_number": r["number_phone"],
            "recording_id": recordings.get(r["id"]),
        }
        for r in call_rows
    ]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    items.sort(key=lambda i: i["at"] or epoch)

    notes = (
        (
            await db.execute(
                select(ContactNote, User.email)
                .join(User, ContactNote.created_by_user_id == User.id, isouter=True)
                .where(ContactNote.caller_id == caller_id)
                .order_by(ContactNote.created_at.desc())
            )
        )
        .all()
    )
    return {
        "contact": {
            "caller_id": str(caller.id),
            "phone_number": caller.phone_number,
            "name": caller.label,
            "company": caller.company,
            "role": caller.role,
            "first_seen_at": caller.first_seen_at,
            "total_calls": caller.total_calls,
        },
        "items": items,
        "notes": [
            {
                "id": str(n.id),
                "body": n.body,
                "author": email,
                "created_at": n.created_at,
            }
            for n, email in notes
        ],
    }


# --- read / open state -------------------------------------------------------------------


async def _upsert_state(db: AsyncSession, caller_id: uuid.UUID, **fields) -> None:
    now = datetime.now(timezone.utc)
    await db.execute(
        pg_insert(ContactThreadState)
        .values(caller_id=caller_id, updated_at=now, **fields)
        .on_conflict_do_update(
            index_elements=["caller_id"], set_={**fields, "updated_at": now}
        )
    )
    await db.commit()


@router.post("/thread/{caller_id}/read")
async def mark_read(
    caller_id: uuid.UUID,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _upsert_state(db, caller_id, last_read_at=datetime.now(timezone.utc))
    return {"ok": True}


class StateBody(BaseModel):
    closed: bool


@router.post("/thread/{caller_id}/state")
async def set_state(
    caller_id: uuid.UUID,
    payload: StateBody,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _upsert_state(
        db, caller_id, closed_at=datetime.now(timezone.utc) if payload.closed else None
    )
    return {"ok": True, "closed": payload.closed}


# --- contact panel -----------------------------------------------------------------------


class ContactUpdate(BaseModel):
    name: str | None = None
    company: str | None = None
    role: str | None = None


@router.patch("/contacts/{caller_id}")
async def update_contact(
    caller_id: uuid.UUID,
    payload: ContactUpdate,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    caller = await db.get(Caller, caller_id)
    if caller is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")
    data = payload.model_dump(exclude_unset=True)
    if "name" in data:
        caller.label = (data["name"] or "").strip() or None
    if "company" in data:
        caller.company = (data["company"] or "").strip() or None
    if "role" in data:
        caller.role = (data["role"] or "").strip() or None
    await db.commit()
    return {
        "caller_id": str(caller.id),
        "name": caller.label,
        "company": caller.company,
        "role": caller.role,
    }


class NoteBody(BaseModel):
    body: str


@router.post("/contacts/{caller_id}/notes")
async def add_note(
    caller_id: uuid.UUID,
    payload: NoteBody,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "note body is required")
    if await db.get(Caller, caller_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")
    note = ContactNote(caller_id=caller_id, body=body, created_by_user_id=user.id)
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return {"id": str(note.id), "body": note.body, "created_at": note.created_at}


@router.delete("/notes/{note_id}")
async def delete_note(
    note_id: uuid.UUID,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    note = await db.get(ContactNote, note_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "note not found")
    await db.delete(note)
    await db.commit()
    return {"ok": True}


# --- settings (default outbound DID) -----------------------------------------------------


@router.get("/settings")
async def get_inbox_settings(
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    numbers = (
        (
            await db.execute(
                select(Number)
                .where(Number.owner_provider == "bulkvs", Number.active.is_(True))
                .order_by(Number.phone_number)
            )
        )
        .scalars()
        .all()
    )
    # A DID the carrier hasn't activated yet (e.g. a SUBMITTED port-in) is not offered as
    # an outbound identity at all.
    numbers = [n for n in numbers if is_carrier_active(n.provider_status)]
    row = await db.get(AppSetting, DEFAULT_NUMBER_KEY)
    default_id = (row.value or {}).get("number_id") if row else None
    return {
        "default_number_id": default_id,
        "numbers": [
            {
                "id": str(n.id),
                "phone_number": n.phone_number,
                "friendly_name": n.friendly_name,
                "sms_ok": sms.outbound_block_reason(n.sms_enabled, n.sms_campaign_id) is None,
            }
            for n in numbers
        ],
    }


class DefaultNumberBody(BaseModel):
    number_id: uuid.UUID | None = None


@router.put("/settings/default-number")
async def set_default_number(
    payload: DefaultNumberBody,
    _: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    value = None
    if payload.number_id is not None:
        number = await db.get(Number, payload.number_id)
        if number is None or number.owner_provider != "bulkvs" or not number.active:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "not an active owned BulkVS number"
            )
        if not is_carrier_active(number.provider_status):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"number is not active at the carrier yet (status: {number.provider_status})",
            )
        value = {"number_id": str(number.id)}
    now = datetime.now(timezone.utc)
    await db.execute(
        pg_insert(AppSetting)
        .values(key=DEFAULT_NUMBER_KEY, value=value, updated_at=now)
        .on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": now})
    )
    await db.commit()
    return {"default_number_id": (value or {}).get("number_id")}


# --- send --------------------------------------------------------------------------------


def _normalize_contact(raw: str) -> str:
    """Light E.164 normalization for operator-typed numbers (new-chat flow)."""
    s = raw.strip()
    digits = re.sub(r"[^\d+]", "", s)
    if digits.startswith("+"):
        return digits
    bare = re.sub(r"\D", "", digits)
    if len(bare) == 10:
        return f"+1{bare}"
    if len(bare) == 11 and bare.startswith("1"):
        return f"+{bare}"
    return digits or s


class InboxSendBody(BaseModel):
    contact: str  # external party's number; normalized server-side
    body: str
    number_id: uuid.UUID | None = None  # explicit from-DID override


@router.post("/send")
async def send(
    payload: InboxSendBody,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Send an SMS resolving the from-DID server-side: explicit override > sticky DID
    (contact's last interaction) > global default. The chosen DID must pass the 10DLC
    gate; the contact must not have opted out from it. 409 with a reason otherwise."""
    body = (payload.body or "").strip()
    if not body:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "message body is required")
    contact = _normalize_contact(payload.contact or "")
    if not contact:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "contact number is required")

    number: Number | None = None
    via_fallback = False
    if payload.number_id is not None:
        number = await db.get(Number, payload.number_id)
        if number is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "number not found")
        reason = sms.outbound_block_reason(number.sms_enabled, number.sms_campaign_id)
        if reason:
            raise HTTPException(status.HTTP_409_CONFLICT, reason)
    else:
        # Sticky: DID of the contact's most recent platform interaction.
        caller = (
            await db.execute(select(Caller).where(Caller.phone_number == contact))
        ).scalar_one_or_none()
        sticky = None
        if caller is not None:
            last_msg = (
                (
                    await db.execute(
                        _msg_stmt()
                        .where(Message.caller_id == caller.id)
                        .order_by(Message.received_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            last_call = (
                (
                    await db.execute(
                        _call_stmt()
                        .where(Call.caller_id == caller.id)
                        .order_by(Call.started_at.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            newest = None
            if last_msg and last_call:
                newest = (
                    last_msg
                    if (last_msg["received_at"] or datetime.min.replace(tzinfo=timezone.utc))
                    >= (last_call["started_at"] or datetime.min.replace(tzinfo=timezone.utc))
                    else last_call
                )
            else:
                newest = last_msg or last_call
            if newest and newest["number_id"]:
                sticky = DidRef(
                    number_id=str(newest["number_id"]),
                    phone_number=newest["number_phone"],
                    sms_enabled=bool(newest["sms_enabled"]),
                    sms_campaign_id=newest["sms_campaign_id"],
                )
        chosen, via_fallback, reason = resolve_sms_from(sticky, await _default_did(db))
        if chosen is None:
            raise HTTPException(status.HTTP_409_CONFLICT, reason or "no SMS-enabled number")
        number = await db.get(Number, uuid.UUID(chosen.number_id))
        if number is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "resolved number no longer exists")

    # Carrier gate covers ALL resolution paths (explicit override, sticky, global default):
    # a DID still provisioning at BulkVS (e.g. SUBMITTED port-in) can never send.
    if not is_carrier_active(number.provider_status):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"number is not active at the carrier yet (status: {number.provider_status})",
        )

    state = await get_optout_state(db, number.id, contact)
    if sms.is_opted_out(state):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "recipient has opted out of SMS from this number"
        )

    msg = await enqueue_outbound_message(db, number, contact, body, user.id)
    await queue.enqueue(db, "message_send", {"message_id": str(msg.id)})
    return {
        "id": str(msg.id),
        "status": msg.status,
        "caller_id": str(msg.caller_id),
        "from_number": number.phone_number,
        "via_fallback": via_fallback,
    }

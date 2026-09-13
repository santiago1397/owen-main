"""The mirror itself: read OpenPhone, hand it to the CRM, never write anything back.

## POLLING, NOT WEBHOOKS — the decision and the honest reason

> AMENDED 2026-09-13: both disqualifiers below were removed by the owner, who registers the
> webhook in Quo's dashboard and supplies the signing secret. `webhook.py` now receives it
> and calls `_mirror_call` / `_mirror_message` exactly as this paragraph anticipated. This
> poll stays on as the backstop. See docs/QUO_WEBHOOK.md.

OpenPhone supports webhooks and they would be the better mechanism. This module polls
anyway, and the reason is not effort:

  **Registering an OpenPhone webhook requires `POST /v1/webhooks` against
  api.openphone.com, and the signing secret is returned only in that call's response.**

Both halves of that are disqualifying here.

  1. The owner's rule, and this module's inherited contract, is that OWEN issues GET
     requests only against OpenPhone. `providers/openphone_client.py` has no `_post` by
     construction and its header says adding one silently removes the guarantee for the
     whole file. A webhook registration is a write. There is no version of it that is not.
  2. Without that response we have no signing secret, so we could not verify a webhook's
     signature — and an unverified public endpoint that files events onto real customers'
     timelines is worse than a poll. "Webhooks if you can verify signatures properly,
     otherwise poll" is the instruction, and the precondition genuinely fails.

A third reason, smaller but real: the endpoint would have to be public on a box that is a
live phone system for a working business, and polling needs no new attack surface at all.

**The seam is left clean rather than closed.** If a human registers the webhook in
OpenPhone's own UI and pastes the secret into configuration, a receiver is a small module
that reuses everything below: `_mirror_call` / `_mirror_message` take a single object and
are already idempotent, so a webhook would be a different way of arriving at the same two
functions, not a different mirror. Nothing in this file would change. That work is NOT done
here — there is no public endpoint and no secret — and the poll would stay on as the
backstop for anything a webhook dropped.

## The constraint that shapes everything: there is no time-based sweep

Spec D11a, verified against the live account: `GET /calls` REJECTS a participant-less query
(HTTP 400), including with a `since`/`createdAfter` filter. You cannot ask OpenPhone "what
happened on this number since X". Every read is "what happened with THIS participant".

So the mirror is participant-driven, and the hard part is not fetching — it is knowing who
to ask about. `participants_in_window` answers that from three sources, most authoritative
first, and degrades instead of failing:

  1. `GET /conversations` — the threads on our line, most-recently-active first. This is the
     only source that can surface a STRANGER, so it is the one that makes a real mirror
     possible rather than a mirror of people we already knew about. UNVERIFIED against the
     live account; see `providers/openphone_client.list_conversations`.
  2. `GET /contacts` — OpenPhone's own address book. VERIFIED (D11a).
  3. OWEN's own `callers`, scoped to the window. Costs no OpenPhone request at all.

If (1) is unavailable the mirror is narrower, and `run_once` says so in its result and its
log rather than reporting a clean run over a set it knows is incomplete.

## Cost, against a measured 10 req/s limit

One request per participant per resource, plus one page of enumeration. A poll over N
participants is ~2N requests; `max_participants` bounds N so a runaway address book cannot
turn a five-minute tick into a rate-limit storm. Transcripts and summaries add up to two
more requests per NEW call only — never per poll, because a call already mirrored is never
fetched again.

## Failure is quiet, and that is deliberate

Every OpenPhone call is wrapped. A 401 (revoked key), a 429 (rate limit) or a connection
failure logs and ends the tick. Nothing raises out of `run_once`, because the thing calling
it is APScheduler inside the worker that also drains the queue for a live phone system, and
a mirror of a system the company is MIGRATING AWAY FROM must never be able to affect it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.integrations.openphone import config as op_config
from app.integrations.openphone import contact_book, push
from app.integrations.openphone.events import MirroredCall, MirroredMessage
from app.integrations.openphone.models import (BACKFILL_SETTING_KEY, OpenPhoneMirrorRow)
from app.models import AppSetting
from app.providers import openphone_client as op

logger = logging.getLogger("integrations.openphone.sync")

# How much the routine poll overlaps itself. Generous on purpose: the window is cheap
# (idempotency makes a re-read free) and a gap is not recoverable without a manual backfill,
# so the trade is entirely one-sided. A poll that runs late, or a worker that restarts, has
# to fall more than an hour behind before anything is missed.
POLL_OVERLAP_SECONDS = 3600

# Bounds the paging loops so a malformed `nextPageToken` cannot spin forever against a
# rate-limited API. At 50 per page this is 5000 objects, far beyond a 30-day window on one
# business line.
MAX_PAGES = 100


def enabled() -> bool:
    """Scheduler gate, matching `workers/bulkvs_sync.enabled`. Checked again inside
    `run_once`, because "the scheduler did not start it" and "it must not run" are two
    different claims and only the second one is a safety property."""
    return op_config.mirror_enabled()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value) -> Optional[datetime]:
    """OpenPhone's RFC-3339 -> an aware UTC datetime, or None if it is not one.

    Never raises. A timestamp we cannot read must not stop a mirror: the object still gets
    mirrored, it is simply not filtered out by the window, which errs toward including a
    customer's message rather than silently dropping it.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _page_items(body: Any) -> list[dict]:
    """The `data` array out of an OpenPhone page, whatever the envelope turns out to be."""
    if isinstance(body, dict):
        items = body.get("data")
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    if isinstance(body, list):
        return [i for i in body if isinstance(i, dict)]
    return []


def _next_token(body: Any) -> Optional[str]:
    if isinstance(body, dict):
        token = body.get("nextPageToken") or body.get("nextPageToken".lower())
        return str(token) if token else None
    return None


# --- who do we ask about? -------------------------------------------------------------


def _numbers_from_contact(entry: dict) -> list[str]:
    """Every phone number on an OpenPhone contact record (D11a's verified shape)."""
    fields = entry.get("defaultFields")
    fields = fields if isinstance(fields, dict) else {}
    out: list[str] = []
    for item in fields.get("phoneNumbers") or []:
        if isinstance(item, dict):
            value = item.get("value") or item.get("number")
        else:
            value = item
        if value:
            out.append(str(value))
    return out


async def _from_conversations(number_id: str, line_key: str, since: datetime,
                              cfg: op_config.MirrorSettings) -> tuple[set[str], bool]:
    """Participants with thread activity since `since`. `(numbers, worked)`.

    `worked` is False when the endpoint is unavailable — which is a real possibility, since
    this is the one read the probe has not confirmed. The caller reports the degradation
    rather than pretending the smaller set was the whole set.
    """
    found: set[str] = set()
    token: Optional[str] = None
    try:
        for _ in range(MAX_PAGES):
            body = await op.list_conversations(number_id, page_token=token,
                                               limit=cfg.page_limit)
            items = _page_items(body)
            if not items:
                break
            exhausted = False
            for conv in items:
                last = _parse_dt(conv.get("lastActivityAt") or conv.get("updatedAt"))
                if last is not None and last < since:
                    # Most-recently-active first, so the first stale thread ends the walk.
                    exhausted = True
                    continue
                for participant in conv.get("participants") or []:
                    key = op_config.match_key(participant)
                    # Never ask OpenPhone about our OWN line: `participants` includes it on
                    # some shapes, and a self-query would mirror the number's whole history
                    # against a contact named after our own phone number.
                    if key and key != line_key:
                        found.add(str(participant))
            token = _next_token(body)
            if exhausted or not token:
                break
        return found, True
    except Exception:  # noqa: BLE001 - degrade to the verified sources
        logger.warning(
            "openphone-mirror: /conversations unavailable — falling back to the address "
            "book and OWEN's own callers. The mirror will be NARROWER than the account "
            "(a stranger with no contact record will be missed).", exc_info=True)
        return found, False


async def _from_address_book(line_key: str,
                             cfg: op_config.MirrorSettings) -> set[str]:
    """Every number in OpenPhone's own contact list. VERIFIED endpoint (D11a)."""
    found: set[str] = set()
    token: Optional[str] = None
    try:
        for _ in range(MAX_PAGES):
            body = await op.list_contacts(page_token=token, limit=cfg.page_limit)
            items = _page_items(body)
            if not items:
                break
            # Keep Quo's names for these numbers, for a CRM thread that is not a contact.
            contact_book.remember(items)
            for entry in items:
                for number in _numbers_from_contact(entry):
                    key = op_config.match_key(number)
                    if key and key != line_key:
                        found.add(number)
            token = _next_token(body)
            if not token:
                break
    except Exception:  # noqa: BLE001 - one source failing is not the mirror failing
        logger.warning("openphone-mirror: /contacts enumeration failed", exc_info=True)
    return found


async def _from_owen_callers(db, since: datetime, line_key: str) -> set[str]:
    """OWEN's own recent callers. Costs no OpenPhone request, and it is the source that
    covers the case the owner cares most about: somebody who rang the BulkVS number AND
    texted the OpenPhone one is the same customer, and their thread should hold both."""
    from app.models import Caller

    found: set[str] = set()
    try:
        rows = (await db.execute(
            select(Caller.phone_number).where(Caller.last_seen_at >= since).limit(1000)
        )).scalars().all()
    except Exception:  # noqa: BLE001 - an optional source, on a table we do not own
        logger.warning("openphone-mirror: OWEN caller enumeration failed", exc_info=True)
        return found
    for number in rows:
        key = op_config.match_key(number)
        if key and key != line_key:
            found.add(str(number))
    return found


async def participants_in_window(db, number_id: str, line_number: str, since: datetime,
                                 cfg: op_config.MirrorSettings) -> tuple[list[str], bool]:
    """Who to ask OpenPhone about. `(participants, complete)`.

    `complete` is False when `/conversations` could not be read, meaning the set is the
    people we already knew about rather than everyone who touched the line. `run_once`
    surfaces that; it is the difference between "mirrored everything" and "mirrored what it
    could see", and reporting the second as the first is the failure this flag exists for.
    """
    line_key = op_config.match_key(line_number)
    from_convos, complete = await _from_conversations(number_id, line_key, since, cfg)
    merged: dict[str, str] = {}

    def add(numbers: Iterable[str]) -> None:
        for number in numbers:
            key = op_config.match_key(number)
            # Keyed by match key so "+19415550123" and "(941) 555-0123" cost ONE request,
            # not two — the same identity rule the CRM files the result under.
            if key and key not in merged:
                merged[key] = number

    add(sorted(from_convos))
    add(sorted(await _from_address_book(line_key, cfg)))
    add(sorted(await _from_owen_callers(db, since, line_key)))

    participants = list(merged.values())
    if len(participants) > cfg.max_participants:
        # Truncation is logged loudly and reported, never silent. The order above is
        # deliberate: conversation participants come first, so what gets dropped is the
        # dormant end of the address book rather than somebody with live activity.
        logger.warning(
            "openphone-mirror: %d participants exceeds OPENPHONE_MIRROR_MAX_PARTICIPANTS="
            "%d — mirroring the %d most recently active. Raise the limit or narrow the "
            "window.", len(participants), cfg.max_participants, cfg.max_participants)
        participants = participants[:cfg.max_participants]
        complete = False
    return participants, complete


# --- mirroring one object -------------------------------------------------------------


async def _already_mirrored(db, kind: str, external_id: str) -> bool:
    row = (await db.execute(
        select(OpenPhoneMirrorRow.id).where(
            OpenPhoneMirrorRow.kind == kind,
            OpenPhoneMirrorRow.external_id == external_id,
        ).limit(1)
    )).first()
    return row is not None


async def _record_and_enqueue(db, *, kind: str, external_id: str, customer_number: str,
                              line_number: str, occurred_at: Optional[datetime],
                              payload: dict) -> str:
    """Write the state row and queue the delivery IN ONE TRANSACTION.

    The order matters and it is the opposite of the obvious one. Enqueue-then-record would
    leave a crash window where the CRM gets the event and OWEN forgets it did — and the next
    poll would send it again. Record-then-enqueue leaves the opposite window, where OWEN
    thinks it sent something it did not, which would LOSE a customer's message.

    Neither is acceptable, so they share a transaction: `queue.enqueue` commits both rows or
    neither. The UNIQUE constraint then handles the only remaining race, two ticks on one
    object, by making the loser fail rather than duplicate.

    Returns "sent" | "duplicate" | "refused".
    """
    db.add(OpenPhoneMirrorRow(
        kind=kind,
        external_id=external_id,
        dedupe_key=op_config.dedupe_key(kind, external_id),
        customer_key=op_config.match_key(customer_number) or None,
        line_number=line_number or None,
        occurred_at=occurred_at,
    ))
    try:
        queued = await push.enqueue_mirrored(db, payload)
    except IntegrityError:
        # The other tick won. Its job is already queued, so this is success, not an error.
        await db.rollback()
        return "duplicate"
    if not queued:
        # Refused on configuration. Roll the state row back, or the mirror would remember
        # having sent something it never queued and never try again.
        await db.rollback()
        return "refused"
    return "sent"


def _customer_number(entry: dict, line_key: str, fallback: str) -> str:
    """The OTHER party on an OpenPhone object.

    `from`/`to` are the reliable pair; `participants` is the fallback. Our own line is
    filtered out by match key, so whichever field holds it, the customer is what is left.
    `fallback` is the participant we asked about, which is correct by construction — we
    only got this object by naming them.
    """
    for value in (entry.get("from"), entry.get("to")):
        if isinstance(value, str) and value:
            key = op_config.match_key(value)
            if key and key != line_key:
                return value
    for value in entry.get("participants") or []:
        key = op_config.match_key(value)
        if key and key != line_key:
            return str(value)
    return fallback


async def _call_extras(call_id: str, cfg: op_config.MirrorSettings) -> tuple[str, str]:
    """`(transcript, summary)` for a call — best effort, never fatal.

    Only ever fetched for a call being mirrored for the FIRST time, so this is two requests
    per new call and zero per poll. OpenPhone has already done the STT (D11a), so carrying
    the transcript costs nothing but the round trip and gives the CRM's thread the content
    of the call, not just its metadata.
    """
    if not cfg.fetch_transcripts:
        return "", ""
    transcript = summary = ""
    try:
        body = await op.get_call_transcript(call_id)
        lines = []
        for seg in (body or {}).get("dialogue") or []:
            if not isinstance(seg, dict):
                continue
            who = str(seg.get("identifier") or "").strip()
            said = str(seg.get("content") or "").strip()
            if said:
                lines.append(f"{who}: {said}" if who else said)
        transcript = "\n".join(lines)
    except Exception:  # noqa: BLE001 - a call with no transcript is still worth mirroring
        logger.debug("openphone-mirror: no transcript for call %s", call_id, exc_info=True)
    try:
        body = await op.get_call_summary(call_id)
        # D11a: the schema is present but came back EMPTY on the sampled call, and the spec
        # says explicitly not to design against `jobs`. So this reads `summary` only, and an
        # empty one simply contributes nothing to the thread line.
        summary = str((body or {}).get("summary") or "").strip()
    except Exception:  # noqa: BLE001
        logger.debug("openphone-mirror: no summary for call %s", call_id, exc_info=True)
    return transcript, summary


async def _mirror_call(db, entry: dict, *, line_number: str, line_key: str,
                       participant: str, since: datetime,
                       cfg: op_config.MirrorSettings, dry_run: bool) -> Optional[str]:
    call_id = str(entry.get("id") or "").strip()
    if not call_id:
        return None
    occurred = _parse_dt(entry.get("createdAt") or entry.get("completedAt"))
    if occurred is not None and occurred < since:
        return None
    if await _already_mirrored(db, "call", call_id):
        return "duplicate"
    if dry_run:
        return "would-send"

    transcript, summary = await _call_extras(call_id, cfg)
    has_recording = bool(entry.get("recordingUrl") or entry.get("recordings")
                         or entry.get("hasRecording"))
    if not has_recording:
        # The list shape does not reliably carry it, so ask — but only for a call we are
        # about to mirror, and only once in its life. A missing recording is not an error.
        try:
            rec = await op.get_call_recording(call_id)
            has_recording = bool((rec or {}).get("url"))
        except Exception:  # noqa: BLE001 - no recording, or none exposed. Mirror anyway.
            has_recording = False

    duration = entry.get("duration")
    try:
        duration = None if duration is None else max(0, int(duration))
    except (TypeError, ValueError):
        duration = None

    facts = MirroredCall(
        external_id=call_id,
        customer_number=_customer_number(entry, line_key, participant),
        line_number=line_number,
        direction=str(entry.get("direction") or "incoming"),
        status=str(entry.get("status") or ""),
        duration_seconds=duration,
        occurred_at=occurred.isoformat() if occurred else None,
        has_recording=has_recording,
        transcript=transcript,
        summary=summary,
    )
    return await _record_and_enqueue(
        db, kind="call", external_id=call_id, customer_number=facts.customer_number,
        line_number=line_number, occurred_at=occurred, payload=facts.as_payload())


async def _mirror_message(db, entry: dict, *, line_number: str, line_key: str,
                          participant: str, since: datetime,
                          dry_run: bool) -> Optional[str]:
    message_id = str(entry.get("id") or "").strip()
    if not message_id:
        return None
    occurred = _parse_dt(entry.get("createdAt") or entry.get("sentAt"))
    if occurred is not None and occurred < since:
        return None
    if await _already_mirrored(db, "message", message_id):
        return "duplicate"
    if dry_run:
        return "would-send"

    media = entry.get("media")
    num_media = len(media) if isinstance(media, list) else 0

    facts = MirroredMessage(
        external_id=message_id,
        customer_number=_customer_number(entry, line_key, participant),
        line_number=line_number,
        direction=str(entry.get("direction") or "incoming"),
        body=str(entry.get("text") or entry.get("body") or ""),
        occurred_at=occurred.isoformat() if occurred else None,
        num_media=num_media,
    )
    return await _record_and_enqueue(
        db, kind="message", external_id=message_id, customer_number=facts.customer_number,
        line_number=line_number, occurred_at=occurred, payload=facts.as_payload())


async def _walk(fetch, *, page_limit: int):
    """Page a participant-scoped OpenPhone listing, yielding entries."""
    token: Optional[str] = None
    for _ in range(MAX_PAGES):
        body = await fetch(token, page_limit)
        items = _page_items(body)
        for entry in items:
            yield entry
        token = _next_token(body)
        if not token or not items:
            return


# --- the tick ---------------------------------------------------------------------------


async def _backfill_done(db) -> bool:
    row = await db.get(AppSetting, BACKFILL_SETTING_KEY)
    return bool(row and (row.value or {}).get("completed_at"))


async def _mark_backfill_done(db, counts: dict) -> None:
    row = await db.get(AppSetting, BACKFILL_SETTING_KEY)
    value = {"completed_at": _now().isoformat(), "counts": dict(counts)}
    if row is None:
        db.add(AppSetting(key=BACKFILL_SETTING_KEY, value=value))
    else:
        row.value = value
    await db.commit()


async def run_once(*, dry_run: bool = False, force_backfill: bool = False) -> dict:
    """One mirror tick. Never raises.

    `dry_run` reads OpenPhone and reports what it WOULD send without writing a state row,
    queuing a job or touching the CRM. That is how the 30-day backfill gets sized before
    anyone turns it on — `manage.py preview`.

    The first non-dry run looks back `backfill_days`; every run after that looks back
    `POLL_OVERLAP_SECONDS`. There is no cursor to corrupt: overlap is free because every
    object is idempotent, and a generous overlap is strictly safer than a precise watermark.
    """
    cfg = op_config.current()
    stop = cfg.refusal()
    if stop:
        # The kill switch, checked before a client is constructed or a session opened.
        return {"ran": False, "reason": stop}

    result: dict[str, Any] = {
        "ran": True, "dry_run": bool(dry_run), "mode": "poll",
        "numbers": [], "participants": 0, "complete": True,
        "calls": 0, "messages": 0, "duplicates": 0, "refused": 0, "errors": [],
    }

    try:
        numbers = await op.list_phone_numbers()
    except Exception as exc:  # noqa: BLE001 - a 401 or an outage ends the tick, quietly
        logger.warning("openphone-mirror: /phone-numbers failed — mirror idle this tick "
                       "(%s). The CRM is unaffected.", type(exc).__name__, exc_info=True)
        return {"ran": False, "reason": f"OpenPhone unreachable: {type(exc).__name__}"}

    mirrored = [n for n in numbers
                if isinstance(n, dict) and cfg.mirrors(n.get("number"))]
    if not mirrored:
        logger.info("openphone-mirror: no line on the account is selected for mirroring "
                    "(%d present)", len(numbers))
        return {**result, "ran": False, "reason": "no mirrored line"}

    async with SessionLocal() as db:
        backfilling = force_backfill or not await _backfill_done(db)
        if backfilling:
            since = _now() - timedelta(days=cfg.backfill_days)
            result["mode"] = "backfill"
        else:
            since = _now() - timedelta(seconds=POLL_OVERLAP_SECONDS)

        for number in mirrored:
            number_id = str(number.get("id") or "")
            line_number = str(number.get("number") or "")
            if not number_id:
                continue
            line_key = op_config.match_key(line_number)
            result["numbers"].append(line_number)

            participants, complete = await participants_in_window(
                db, number_id, line_number, since, cfg)
            result["participants"] += len(participants)
            result["complete"] = result["complete"] and complete

            for participant in participants:
                for kind, fetch in (
                    ("call", lambda t, lim, p=participant: op.list_calls_with(
                        number_id, p, page_token=t, limit=lim)),
                    ("message", lambda t, lim, p=participant: op.list_messages(
                        number_id, p, page_token=t, limit=lim)),
                ):
                    try:
                        async for entry in _walk(fetch, page_limit=cfg.page_limit):
                            if kind == "call":
                                outcome = await _mirror_call(
                                    db, entry, line_number=line_number, line_key=line_key,
                                    participant=participant, since=since, cfg=cfg,
                                    dry_run=dry_run)
                            else:
                                outcome = await _mirror_message(
                                    db, entry, line_number=line_number, line_key=line_key,
                                    participant=participant, since=since, dry_run=dry_run)
                            if outcome in (None,):
                                continue
                            if outcome == "duplicate":
                                result["duplicates"] += 1
                            elif outcome == "refused":
                                result["refused"] += 1
                            else:
                                result["calls" if kind == "call" else "messages"] += 1
                    except Exception as exc:  # noqa: BLE001 - one participant, not the tick
                        # Deliberately per-participant-per-resource: a single malformed
                        # object or a transient 429 must not cost the other 199 customers
                        # their mirror for this tick.
                        note = f"{kind}s for one participant: {type(exc).__name__}"
                        if note not in result["errors"]:
                            result["errors"].append(note)
                        logger.warning("openphone-mirror: %s listing failed for one "
                                       "participant", kind, exc_info=True)

        if backfilling and not dry_run and not result["errors"] and result["complete"]:
            # Only close the backfill on a CLEAN, COMPLETE pass. A partial one stays in
            # backfill mode so the next tick looks back the full window again rather than
            # narrowing to an hour and leaving a permanent hole in the history.
            await _mark_backfill_done(db, {
                "calls": result["calls"], "messages": result["messages"],
                "days": cfg.backfill_days,
            })
            result["backfill_closed"] = True

    logger.info("openphone-mirror: %s tick — %d calls, %d messages, %d already mirrored, "
                "%d participants%s", result["mode"], result["calls"], result["messages"],
                result["duplicates"], result["participants"],
                "" if result["complete"] else " (INCOMPLETE participant set)")
    return result


async def poll() -> None:
    """The APScheduler entry point. Swallows everything: see the module docstring."""
    if not enabled():
        return
    try:
        await run_once()
    except Exception:  # noqa: BLE001 - the mirror must never disturb the worker
        logger.exception("openphone-mirror: poll failed")

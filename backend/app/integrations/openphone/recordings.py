"""Repair: give the calls already mirrored to the CRM the recording they were sent without.

## Why this exists (measured on production, 2026-09-14)

Every one of the 499 Quo calls mirrored before this fix reached the CRM with
`recording_url = NULL`. `GET /v1/call-recordings/{id}` answers `{"data": [ ... ]}` — a LIST
— and `openphone_client.get_call_recording` handed that list to callers that did
`.get("url")` on it. The poll swallowed the AttributeError and reported "no recording" for
every call. The client is fixed (`pick_recording`), so new calls are right; but those 499
are marked mirrored and the poll never looks at them again. This walks them once.

## What it does, per mirrored call

  1. Skip it without a request if a `call_recording` state row exists — the recording was
     already sent once (by the webhook's `call.recording.completed`, or by an earlier run
     of this command). That row is what makes a second `--commit` enqueue nothing.
  2. `GET /call-recordings/{id}`. No audio: counted, nothing else happens.
  3. Audio, and `--commit`: `GET /calls/{id}` for the call's real facts, then write the
     `call_recording` state row and ONE `crm_report` job in one transaction — exactly the
     webhook's recording-part path. The job carries the call's SAME dedupe key
     (`openphone:call:<id>`), so the CRM finds its existing row, on a contact thread or a
     number-only thread, and fills the blank `recording_url` (`ENRICHABLE_FIELDS`). It
     overwrites nothing and adds no row.

A dry run (the default) does steps 1 and 2 only: it reads Quo and writes nothing, anywhere.

## Pacing — two limits, both real

  * Quo allows ~10 requests/second per key, and the live poll shares that key. Requests
    here are spaced `quo_interval` apart (default 0.25 s, so at most 4/s).
  * Each job is delivered through OWEN's own `/api/openphone-mirror/events` on the agent
    key, which is limited to `AI_API_RATE_LIMIT_PER_MIN` (60) per minute — shared with the
    live CRM link. The first backfill hit that 429. So jobs are STAGGERED with
    `run_after`: job n is due `n * spacing_seconds` from now (default 3 s, 20/min), leaving
    two thirds of the budget for live traffic, and the queue never holds a burst.

## Never a write to Quo

Every Quo request goes through `providers/openphone_client`, which has no write method.
`tests/test_openphone_recordings.py` drives a full commit against a recording transport and
asserts zero non-GET requests.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from sqlalchemy import select

from app.integrations.openphone import config as op_config
from app.integrations.openphone import push
from app.integrations.openphone import sync
from app.integrations.openphone import webhook
from app.integrations.openphone.events import MirroredCall
from app.integrations.openphone.models import OpenPhoneMirrorRow

logger = logging.getLogger("integrations.openphone.recordings")

# The state-row kind the webhook writes for `call.recording.completed`. Shared on purpose:
# a recording sent by either path is never sent by the other.
PART_KIND = "call_recording"

DEFAULT_QUO_INTERVAL = 0.25
DEFAULT_SPACING_SECONDS = 3


async def _mirrored_calls(db) -> list[Any]:
    return list((await db.execute(
        select(OpenPhoneMirrorRow.external_id, OpenPhoneMirrorRow.customer_key,
               OpenPhoneMirrorRow.line_number, OpenPhoneMirrorRow.occurred_at)
        .where(OpenPhoneMirrorRow.kind == "call")
        .order_by(OpenPhoneMirrorRow.occurred_at)
    )).all())


def _facts(call_id: str, call: dict, line_number: str, customer_key: Optional[str],
           fallback_occurred) -> MirroredCall:
    """The call as the webhook's recording-part path describes it: real direction, status
    and duration from `GET /calls/{id}`, `has_recording=True`, no transcript or summary
    (those were sent with the call, or by their own webhook part)."""
    entry = webhook.call_entry(call)
    line_key = op_config.match_key(line_number)
    customer = webhook._customer(call, line_key) or op_config.to_e164(customer_key or "")
    occurred = sync._parse_dt(entry.get("createdAt") or entry.get("completedAt"))
    occurred = occurred or fallback_occurred
    duration = entry.get("duration")
    try:
        duration = None if duration is None else max(0, int(duration))
    except (TypeError, ValueError):
        duration = None
    return MirroredCall(
        external_id=call_id,
        customer_number=customer,
        line_number=line_number,
        direction=str(entry.get("direction") or "incoming"),
        status=str(entry.get("status") or ""),
        duration_seconds=duration,
        occurred_at=occurred.isoformat() if occurred else None,
        has_recording=True,
    )


async def repair(*, commit: bool = False, session_factory=None,
                 quo_interval: float = DEFAULT_QUO_INTERVAL,
                 spacing_seconds: int = DEFAULT_SPACING_SECONDS,
                 sleep=asyncio.sleep) -> dict:
    """Walk every mirrored call once. Returns counts, and nothing that names a customer.

    `{"ran", "commit", "checked", "with_audio", "without_audio", "already_sent", "errors",
    "enqueued"}`. Never raises for one call's failure; that call is counted in `errors` and
    a later run tries it again (it has no state row).
    """
    from app.providers import openphone_client as op

    cfg = op_config.current()
    stop = cfg.refusal() or (push.refusal() if commit else None)
    if stop:
        return {"ran": False, "commit": bool(commit), "reason": stop}

    counts = {"ran": True, "commit": bool(commit), "checked": 0, "with_audio": 0,
              "without_audio": 0, "already_sent": 0, "errors": 0, "enqueued": 0}
    factory = session_factory or sync.SessionLocal
    first_request = True

    async def paced(fn, *args):
        nonlocal first_request
        if not first_request and quo_interval > 0:
            await sleep(quo_interval)
        first_request = False
        return await fn(*args)

    async with factory() as db:
        for row in await _mirrored_calls(db):
            call_id, customer_key, line_number, occurred = (row[0], row[1], row[2], row[3])
            call_id = str(call_id or "").strip()
            if not call_id:
                continue
            counts["checked"] += 1
            if await sync._already_mirrored(db, PART_KIND, call_id):
                counts["already_sent"] += 1
                continue
            try:
                rec = await paced(op.get_call_recording, call_id)
            except Exception as exc:  # noqa: BLE001 - one call's failure is counted, not fatal
                counts["errors"] += 1
                logger.warning("openphone-recordings: lookup failed for %s (%s)",
                               call_id, sync.describe_error(exc))
                continue
            if not rec.get("url"):
                counts["without_audio"] += 1
                continue
            counts["with_audio"] += 1
            if not commit:
                continue

            try:
                call = await paced(webhook._fetch_call, op, call_id)
            except Exception as exc:  # noqa: BLE001
                counts["errors"] += 1
                logger.warning("openphone-recordings: call detail failed for %s (%s)",
                               call_id, sync.describe_error(exc))
                continue
            facts = _facts(call_id, call, str(line_number or ""), customer_key, occurred)
            outcome = await sync._record_and_enqueue(
                db, kind=PART_KIND, external_id=call_id,
                customer_number=facts.customer_number, line_number=facts.line_number,
                occurred_at=sync._parse_dt(facts.occurred_at), payload=facts.as_payload(),
                delay_seconds=counts["enqueued"] * max(0, int(spacing_seconds)))
            if outcome == "sent":
                counts["enqueued"] += 1
            elif outcome == "duplicate":
                counts["already_sent"] += 1
            else:
                counts["errors"] += 1
    return counts

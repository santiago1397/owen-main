"""Enqueue CRM event pushes onto the EXISTING `crm_report` job.

## Why this reuses `crm_report` instead of posting anything itself

`workers/handlers.py::handle_crm_report` already does the delivery: it POSTs
`{url, headers, body}` from a job payload to any URL and raises on a 4xx/5xx so the
Postgres queue retries it with linear backoff and dead-letters it after five attempts. It
is deliberately CRM-agnostic. Writing a second delivery mechanism would mean writing a
second retry policy, a second backoff, and a second thing that can silently stop working.

## Why the job posts to OWEN and not to the CRM

Two reasons, both load-bearing:

  1. **Reachability.** `handle_crm_report` runs in the WORKER container, and
     `docker-compose.prod.yml` puts `worker` on `callmon-net` only — NOT on
     `traefik-public`, which is the network `callmon_app` and `ghl_clone_api` share. The
     worker therefore cannot resolve `ghl_clone_api` at all. The `app` container is on both,
     so app -> CRM works over Docker DNS with no public round trip, and worker -> app works
     over `callmon-net`.
  2. **The token.** A job payload is a row in the `jobs` table. Putting a `ghl_pat_...`
     bearer in `headers` would persist a live CRM credential in Postgres, where `pg_dump`
     and the `owen_ro` role behind `/api/ai/query` can both see it. Resolving the token at
     the app-side adapter keeps it in the app container's environment.

This is exactly the hop `flows/runtime.py::_enqueue_crm_report` already uses for the
built-in `kind: ghl` adapter (`OWEN_INTERNAL_URL` + `X-OWEN-Key`), so it is an established
seam rather than a new one.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from app.core.config import settings
from app.db import SessionLocal
from app.integrations.crm import config as crm_config
from app.integrations.crm.events import CallEventFacts
from app.models import Call, Recording, Transcription
from app.services import queue

logger = logging.getLogger("integrations.crm.push")

PROVIDER_NAME = "asterisk"  # matches flows/runtime.py + workers/asterisk_consumer.py

# The job type is the platform's existing CRM-agnostic reporter. Not a new handler.
JOB_TYPE = "crm_report"


def delivery_url() -> str:
    """Where the worker posts. See the module docstring for why this is OWEN and not the CRM."""
    return f"{settings.OWEN_INTERNAL_URL.rstrip('/')}/api/crm-link/events"


async def call_artifacts(db, linkedid: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """`(calls.id, recordings.id, transcriptions.id)` for a Linkedid — whichever exist yet.

    Recording and transcript are looked up rather than assumed: at `started` neither exists,
    at `ended` the recording usually does and the transcript almost never does (it is
    produced by the `recording_fetch -> transcribe` chain minutes later). Reporting the ids
    we have and null for the rest is the honest answer; waiting for the transcript would
    hold the CRM timeline hostage to a pipeline that runs on its own schedule.
    """
    try:
        call = (
            await db.execute(
                select(Call).where(Call.provider_call_sid == str(linkedid)).limit(1)
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - never let a lookup break the call path
        logger.exception("crm-link: call lookup failed for linkedid=%s", linkedid)
        return None, None, None
    if call is None:
        return None, None, None

    rec_id = trans_id = None
    try:
        rec = (
            await db.execute(
                select(Recording).where(Recording.call_id == call.id)
                .order_by(Recording.id).limit(1)
            )
        ).scalar_one_or_none()
        rec_id = str(rec.id) if rec is not None else None
        trans = (
            await db.execute(
                select(Transcription).where(Transcription.call_id == call.id)
                .order_by(Transcription.id).limit(1)
            )
        ).scalar_one_or_none()
        trans_id = str(trans.id) if trans is not None else None
    except Exception:  # noqa: BLE001 - artifacts are a nice-to-have on the timeline entry
        logger.exception("crm-link: artifact lookup failed for linkedid=%s", linkedid)
    return str(call.id), rec_id, trans_id


async def enqueue_call_event(facts: CallEventFacts) -> bool:
    """Queue one lifecycle event for delivery. Returns True iff a job was written.

    Refuses (returning False, loudly at INFO) when the kill switch is off or no token is
    configured. Never raises: this is called from the middle of a live call, and a CRM that
    cannot be told about the call is not a reason to drop the call.
    """
    cfg = crm_config.current()
    refusal = cfg.delivery_refusal()
    if refusal:
        logger.info("crm-link: not reporting %s for %s — %s",
                    facts.phase, facts.linkedid or facts.owen_call_id, refusal)
        return False
    if not settings.AGENT_RUNTIME_KEY:
        # The worker authenticates to OWEN's own adapter with this key. Without it the job
        # would be written, posted, 401'd, retried five times and dead-lettered.
        logger.warning(
            "crm-link: AGENT_RUNTIME_KEY is unset; cannot report %s for %s "
            "(mint a key with the 'crm_link' scope and set AGENT_RUNTIME_KEY)",
            facts.phase, facts.linkedid or facts.owen_call_id,
        )
        return False

    try:
        async with SessionLocal() as db:
            await queue.enqueue(db, JOB_TYPE, {
                "url": delivery_url(),
                "headers": {"X-OWEN-Key": settings.AGENT_RUNTIME_KEY},
                "body": facts.as_payload(),
            })
        logger.info("crm-link: queued %s event for call %s (linkedid=%s)",
                    facts.phase, facts.owen_call_id, facts.linkedid)
        return True
    except Exception:  # noqa: BLE001 - reporting must never affect the call
        logger.exception("crm-link: queuing the %s event failed (linkedid=%s)",
                         facts.phase, facts.linkedid)
        return False


async def report_call_phase(
    *, phase: str, linkedid: str, binding, caller_number: str, dialed_number: str,
    direction: str = "inbound", outcome: str = "", duration_seconds: int | None = None,
    winning_destination: str | None = None, winning_kind: str | None = None,
    extra: dict | None = None,
) -> bool:
    """Resolve this call's ids and queue one lifecycle event.

    Opens its own short-lived session (the runtime's session-per-write pattern) because the
    call this reports on may still be up and holding a transaction open across a bridge is
    the mistake `flows/runtime.py` documents avoiding.
    """
    owen_call_id = rec_id = trans_id = None
    try:
        async with SessionLocal() as db:
            owen_call_id, rec_id, trans_id = await call_artifacts(db, linkedid)
    except Exception:  # noqa: BLE001
        logger.exception("crm-link: could not resolve call artifacts (linkedid=%s)", linkedid)

    if not owen_call_id:
        # The StasisStart status event creates the `calls` row before the flow runtime runs,
        # so this should not happen — but `calls.id` IS the join key the CRM stores as
        # `owen_call_id`, and an event without it is a timeline entry nobody can join back.
        # Report it anyway keyed on the Linkedid, and say so.
        logger.warning("crm-link: no calls row for linkedid=%s; reporting without calls.id",
                       linkedid)

    link_url = ""
    if settings.OWEN_CALL_URL_TEMPLATE:
        link_url = settings.OWEN_CALL_URL_TEMPLATE.replace("{linkedid}", str(linkedid))

    facts = CallEventFacts(
        phase=phase,
        owen_call_id=owen_call_id or "",
        linkedid=str(linkedid),
        caller_number=caller_number or "",
        dialed_number=dialed_number or "",
        direction=direction,
        outcome=outcome or "",
        duration_seconds=duration_seconds,
        winning_destination=winning_destination,
        winning_kind=winning_kind,
        recording_id=rec_id,
        transcript_id=trans_id,
        owen_url=link_url or None,
        extra={
            "crm_link_id": getattr(binding, "link_id", None),
            "crm_base_url": getattr(binding, "crm_base_url", None),
            **(extra or {}),
        },
    )
    return await enqueue_call_event(facts)


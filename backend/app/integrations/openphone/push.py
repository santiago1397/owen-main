"""Enqueue mirrored OpenPhone activity onto the EXISTING `crm_report` job.

Reuses the delivery mechanism `integrations/crm/push.py` established, for the reasons that
module spells out in full and which have not changed:

  * `workers/handlers.py::handle_crm_report` already POSTs `{url, headers, body}` and raises
    on 4xx/5xx, so the Postgres queue gives this retry, linear backoff and a dead letter
    after five attempts for free. A second delivery mechanism would mean a second retry
    policy and a second thing that can silently stop working.
  * The job posts to OWEN, not to the CRM, because the WORKER container is on `callmon-net`
    only and cannot resolve `ghl_clone_api` at all — and because a job payload is a row in
    `jobs`, where a `ghl_pat_...` bearer would be visible to `pg_dump` and to the `owen_ro`
    role behind `/api/ai/query`. The token is resolved at the app-side adapter.

One difference from the CRM link's version, and it is the important one: **nothing here runs
on a live call path.** `crm/push.enqueue_call_event` is called from the middle of a ringing
phone and therefore swallows every exception, because a CRM that cannot be told about a call
is not a reason to drop the call. This module is called from a scheduled poll, where the
honest response to a failure is to let it raise, log, and try again on the next tick —
`sync.py` is the one place that decides a failed mirror is survivable.
"""

from __future__ import annotations

import logging

from app.core.config import settings
from app.integrations.openphone import config as op_config
from app.services import queue

logger = logging.getLogger("integrations.openphone.push")

# The platform's existing CRM-agnostic reporter. Not a new handler, not a new job type.
JOB_TYPE = "crm_report"

# The app-side adapter the worker posts back into. One route, because — unlike the CRM
# link's three — every mirrored object maps onto the same CRM endpoint and differs only by
# a `kind` field the pure layer already dispatches on.
MIRROR_EVENT_PATH = "/api/openphone-mirror/events"


def delivery_url() -> str:
    """Where the worker posts. OWEN's own app container — see the module docstring."""
    return f"{settings.OWEN_INTERNAL_URL.rstrip('/')}{MIRROR_EVENT_PATH}"


def refusal() -> str | None:
    """The configuration refusal that stops a push, or None to proceed."""
    cfg = op_config.current()
    stop = cfg.refusal()
    if stop:
        return stop
    if not settings.AGENT_RUNTIME_KEY:
        # The worker authenticates to OWEN's own adapter with this key. Without it the job
        # would be written, posted, 401'd, retried five times and dead-lettered — 5 wasted
        # attempts per mirrored call, which on a backfill is thousands.
        return op_config.REFUSE_NO_AGENT_KEY
    return None


async def enqueue_mirrored(db, payload: dict) -> bool:
    """Queue one mirrored call or text for delivery. True iff a job was written.

    Takes the session rather than opening its own: `sync.py` writes the
    `openphone_mirror_rows` row and enqueues the job in ONE transaction, so a crash between
    the two cannot leave a row claiming something was sent that never was. Getting that
    backwards is how a mirror silently drops a customer's text forever.
    """
    stop = refusal()
    if stop:
        logger.info("openphone-mirror: not reporting %s %s — %s",
                    payload.get("kind"), payload.get("external_id"), stop)
        return False
    await queue.enqueue(db, JOB_TYPE, {
        "url": delivery_url(),
        "headers": {"X-OWEN-Key": settings.AGENT_RUNTIME_KEY},
        "body": dict(payload or {}),
    })
    return True

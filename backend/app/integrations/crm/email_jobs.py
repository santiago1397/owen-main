"""AHS work-order emails -> CRM cards. A SECOND destination, never a replacement (2026-09-14).

`workers/mail_poller.py` stores every Dispatch email and enqueues `email_relay_ghl` for the
ones worth relaying. That path is UNCHANGED by this module: not one line of
`handle_email_relay_ghl`, `_relay_cancellation`, `_relay_via_api` or `services/emails.py`
was edited to add it. What is added:

  * the poller, AFTER its GHL enqueue, calls `enqueue_for_new_email` for a newly-inserted
    parsed work order or cancellation. That stamps `inbound_emails.crm_status = 'queued'`
    and enqueues a job of its own type, `email_relay_crm`.
  * the worker's `handle_email_relay_crm` posts the email id to the app-side adapter
    `POST /api/crm-link/email-jobs` (the worker is on `callmon-net` only and cannot reach
    the CRM; the token lives in the app container — the same hop and the same reasons
    recorded in `push.py`).
  * the adapter builds the CRM body from the stored row, posts it to the CRM with
    CRM_LINK_BASE_URL + CRM_LINK_TOKEN, and records the outcome on the row.

## Independence from the GHL relay

Two job rows, two handlers, two sets of columns. The queue drains and retries each job on
its own, so a CRM that is down retries `email_relay_crm` with backoff while
`email_relay_ghl` completes, and a GHL failure never touches a `crm_*` column. The CRM
enqueue runs after the GHL one has committed and cannot raise into the poller.

## No backfill — structural, not a date comparison

The job acts ONLY on a row whose `crm_status` is 'queued' (or 'failed', its own retry).
Nothing but `enqueue_for_new_email` ever writes 'queued', and the poller calls it only when
`emails.ingest_email` reports `created=True` — the RFC Message-ID was never seen before.
Every email stored before the switch was on has `crm_status` NULL (the migration adds the
column and writes no row) and is refused by the job even if someone enqueues it by hand.
The GHL re-relay paths (`/api/emails/{id}/relay`, `scripts/restore_relay.py`,
`scripts/manage.py`) enqueue `email_relay_ghl` only, so re-relaying an old email to GHL
does not reach the CRM either.

## The switch

`CRM_LINK_EMAIL_JOBS_ENABLED` (default False) AND `CRM_LINK_ENABLED` AND a token. Off, the
poller enqueues nothing and stamps nothing; a job already queued when it is switched off
records 'skipped_disabled' and posts nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

logger = logging.getLogger("integrations.crm.email_jobs")

JOB_TYPE = "email_relay_crm"
ADAPTER_PATH = "/api/crm-link/email-jobs"
CRM_JOB_PATH = "/api/ahs-jobs"
CRM_CANCELLATION_PATH = "/api/ahs-jobs/cancellations"

PARSED = "parsed"
CANCELLATION = "cancellation"

QUEUED = "queued"
FAILED = "failed"
# The only states a job may act on. Anything else is either finished or never queued.
ACTIONABLE = frozenset({QUEUED, FAILED})

REFUSE_SWITCH = "AHS email jobs are off (CRM_LINK_EMAIL_JOBS_ENABLED=false)"


# --- pure helpers ---------------------------------------------------------------------------

def to_cents(total) -> int:
    """The email's payment total as integer cents. Decimal, never float: "125" -> 12500,
    "1,234.56" -> 123456. Anything unreadable or negative is 0 — a missing value must not
    stop a work order becoming a card."""
    if total is None:
        return 0
    try:
        value = Decimal(str(total).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return 0
    if not value.is_finite() or value < 0:
        return 0
    return int((value * 100).to_integral_value())


def refusal(settings) -> Optional[str]:
    """Why a CRM delivery may not be queued or sent right now, or None."""
    from app.integrations.crm import config as crm_config

    if not bool(getattr(settings, "CRM_LINK_EMAIL_JOBS_ENABLED", False)):
        return REFUSE_SWITCH
    cfg = crm_config.settings_view(settings)
    reason = cfg.delivery_refusal()
    if reason:
        return reason
    if not getattr(settings, "AGENT_RUNTIME_KEY", ""):
        return "AGENT_RUNTIME_KEY is unset"
    return None


def should_deliver(parse_status: Optional[str]) -> bool:
    return parse_status in (PARSED, CANCELLATION)


def _iso(value) -> Optional[str]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return None


def job_body(em) -> dict:
    """`POST /api/ahs-jobs` for a parsed work order. The note is the SAME job description
    GoHighLevel's relay attaches (`emails.ghl_payload(em)["job_description"]`)."""
    from app.services import emails

    fields = em.fields or {}
    return {
        "ahs_job_id": str(fields.get("job_id") or em.job_id or ""),
        "service": fields.get("service"),
        "customer_name": fields.get("customer_name"),
        "phone": fields.get("customer_phone"),
        "email": fields.get("customer_email"),
        "service_address": fields.get("service_address"),
        "value_cents": to_cents((fields.get("payment") or {}).get("total")),
        "description": emails.ghl_payload(em).get("job_description"),
        "message_id": em.message_id,
        "received_at": _iso(em.received_at),
    }


def cancellation_body(em) -> dict:
    fields = em.fields or {}
    return {
        "ahs_job_id": str(fields.get("cancelled_job_id") or em.job_id or ""),
        "message_id": em.message_id,
        "received_at": _iso(em.received_at),
    }


# CRM outcome -> the status recorded on the email.
_JOB_STATUS = {"created": "sent", "existing": "existing"}
_CANCEL_STATUS = {"noted": "cancellation_noted",
                  "already_noted": "cancellation_already_noted",
                  "no_card": "skipped_no_card"}


def status_for(kind: str, outcome: Optional[str]) -> str:
    table = _CANCEL_STATUS if kind == CANCELLATION else _JOB_STATUS
    return table.get(str(outcome or ""), "sent" if kind != CANCELLATION else "cancellation_noted")


def result_summary(data: Optional[dict]) -> dict:
    """What is kept of the CRM's answer: ids and the outcome, never the customer."""
    data = data or {}
    opp = data.get("opportunity") if isinstance(data.get("opportunity"), dict) else {}
    contact = data.get("contact") if isinstance(data.get("contact"), dict) else {}
    return {
        "outcome": data.get("outcome"),
        "ahs_job_id": data.get("ahs_job_id"),
        "opportunity_id": opp.get("id") if opp else data.get("opportunity_id"),
        "contact_id": contact.get("id"),
        "matched_by": contact.get("matched_by"),
        "note_id": data.get("note_id"),
    }


# --- the poller's half ----------------------------------------------------------------------

async def enqueue_for_new_email(db, row, parse_status: Optional[str], *, created: bool) -> bool:
    """Queue a CRM delivery for an email the poller has JUST inserted. True iff queued.

    Never raises: the poller has already stored the email and queued its GHL relay, and a
    CRM that cannot be told must not leave the message UNSEEN (a re-poll would find the
    Message-ID stored, `created=False`, and never queue it anyway).
    """
    if not created or not should_deliver(parse_status):
        return False
    try:
        from app.core.config import settings
        from app.services import queue

        reason = refusal(settings)
        if reason:
            logger.debug("email_relay_crm: not queued for %s — %s", row.message_id, reason)
            return False
        # The stamp and the job are ONE commit (`queue.enqueue` adds the job and commits), so
        # there is never a 'queued' row without a job or a job for an unstamped row.
        row.crm_status = QUEUED
        row.crm_error = None
        await queue.enqueue(db, JOB_TYPE, {"email_id": str(row.id)})
        logger.info("email_relay_crm: queued %s (job_id=%s)", row.message_id, row.job_id)
        return True
    except Exception:  # noqa: BLE001 - the GHL relay is already queued; never undo that
        logger.exception("email_relay_crm: queuing failed for %s",
                         getattr(row, "message_id", "?"))
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False

"""Mailbox poller (APScheduler job on the worker).

Every INBOUND_MAIL_POLL_SECONDS: pull UNSEEN mail from the Dispatch sender, parse each,
store idempotently (raw always kept), and enqueue a GHL relay job for newly-inserted
*parsed* emails. Blocking IMAP work runs off the event loop via asyncio.to_thread.

Idempotency has two layers: (1) DB upsert on the RFC Message-ID (the real guard), and
(2) marking handled messages \\Seen so they aren't re-fetched. A message is only marked
\\Seen after it's safely persisted; if a DB write raises, the message stays UNSEEN and is
retried next poll.
"""

import asyncio
import logging

from app.core.config import settings
from app.db import SessionLocal
from app.providers import dispatch_email
from app.services import emails, queue
from app.services import mailbox

logger = logging.getLogger("worker.mail_poller")


def enabled() -> bool:
    return bool(settings.INBOUND_MAIL_HOST and settings.INBOUND_MAIL_USER)


async def poll_mailbox() -> None:
    if not enabled():
        return

    try:
        fetched = await asyncio.to_thread(
            mailbox.fetch_from_sender,
            settings.INBOUND_MAIL_HOST, settings.INBOUND_MAIL_PORT,
            settings.INBOUND_MAIL_USER, settings.INBOUND_MAIL_PASSWORD,
            settings.INBOUND_MAIL_FOLDER, settings.INBOUND_MAIL_SENDER,
            settings.INBOUND_MAIL_BATCH,
        )
    except Exception:  # noqa: BLE001 - a connect/login/search failure retries next poll
        logger.exception("mail_poller: fetch failed")
        return

    if not fetched:
        return

    handled_uids: list[bytes] = []
    relayed = failed = 0
    async with SessionLocal() as db:
        for msg in fetched:
            if not msg.message_id:
                logger.warning("mail_poller: skipping message with no Message-ID (uid=%s)", msg.uid)
                continue
            # Defense in depth: the IMAP SEARCH already filtered by sender.
            if not dispatch_email.matches(msg.from_addr):
                handled_uids.append(msg.uid)  # not ours — mark seen so we skip it next time
                continue
            try:
                parsed = dispatch_email.parse(msg.subject, msg.text_body, msg.html_body)
                row, created = await emails.ingest_email(
                    db, msg, parsed, dispatch_email.SOURCE
                )
                if created and parsed.ok:
                    await queue.enqueue(db, "email_relay_ghl", {"email_id": str(row.id)})
                    relayed += 1
                elif created:
                    failed += 1
                    logger.warning("mail_poller: parse failed for job_id=%s: %s",
                                   parsed.job_id, parsed.error)
                handled_uids.append(msg.uid)
            except Exception:  # noqa: BLE001 - leave UNSEEN so it retries next poll
                logger.exception("mail_poller: failed to persist message_id=%s", msg.message_id)

    if settings.INBOUND_MAIL_MARK_SEEN and handled_uids:
        try:
            await asyncio.to_thread(
                mailbox.mark_seen,
                settings.INBOUND_MAIL_HOST, settings.INBOUND_MAIL_PORT,
                settings.INBOUND_MAIL_USER, settings.INBOUND_MAIL_PASSWORD,
                settings.INBOUND_MAIL_FOLDER, handled_uids,
            )
        except Exception:  # noqa: BLE001 - non-fatal; DB dedupe still prevents reprocessing
            logger.warning("mail_poller: mark_seen failed for %d uids", len(handled_uids))

    logger.info("mail_poller: handled=%d relayed=%d parse_failed=%d",
                len(handled_uids), relayed, failed)

"""Turn already-stored carrier delivery receipts into receipts (2026-09-16).

BulkVS posts delivery receipts to the MO webhook. Until `webhooks/bulkvs.py` learned to
recognise them, every one was stored as an INBOUND MESSAGE — shown in the Inbox as though
the customer had written it, and relayed onward to the CRM, where it landed in that
customer's own conversation. This repairs the rows already in the table.

For each stored receipt it:

  1. parses it with the same parser the live webhook uses — no second definition of what a
     receipt is, so the command and the webhook cannot drift;
  2. correlates it to the outbound message it is about (`services/dlr.correlate`) and
     applies it: the outbound row's status advances forward-only, and the receipt reaches
     the CRM if the CRM sent that message;
  3. marks the junk row so it is hidden from every thread (`services/dlr_junk`).

**It never deletes anything.** Not the junk row and certainly not a message: the detection
is a strict regex, and the one thing that must never happen is a customer's text
disappearing because a pattern was too greedy. Hiding is reversible — clear the marker and
the row is back.

**Idempotent.** A receipt already applied to its message is recognised by id and outcome and
changes nothing; a row already marked is skipped. Run it as often as you like.

**Dry run by default.** Counts only — no phone number, no message body, no customer name
reaches the output, so the report is safe to paste into a ticket.

    docker compose exec -T app python -m app.scripts.backfill_dlrs
    docker compose exec -T app python -m app.scripts.backfill_dlrs --apply
    docker compose exec -T app python -m app.scripts.backfill_dlrs --apply --no-crm

`--no-crm` applies to the local `messages` rows only and tells the CRM nothing. Use it if
the CRM is down or not yet deployed; the receipts it skips are not lost, because a later run
without the flag finds them again — `bulkvs_dlr_seen` records that a receipt was applied,
and the relay is re-attempted for any row whose CRM hop has not been recorded.
"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Message
from app.providers.bulkvs import parse_delivery_receipt
from app.services import dlr
from app.services.dlr_junk import DLR_JUNK_KEY, is_junk


def _as_payload(message: Message) -> dict:
    """The MO payload this row was ingested from, or a reconstruction of it.

    `raw_payload` holds the original webhook body for every inbound row, which is what the
    parser wants. A row whose payload was lost is reconstructed from the columns so it can
    still be read — the body is the part that identifies a receipt, and that is a column.
    """
    raw = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    if raw.get("Message") or raw.get("Body") or raw.get("message") or raw.get("body"):
        return raw
    return {"From": message.from_number, "To": message.to_number, "Message": message.body}


async def run(apply: bool, relay: bool) -> int:
    found = marked = applied = already = orphan = 0
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Message)
                .where(Message.direction == "inbound")
                .order_by(Message.received_at)
            )
        ).scalars().all()

        for message in rows:
            receipt = parse_delivery_receipt(_as_payload(message))
            if receipt is None:
                continue                      # an ordinary customer text. Left alone.
            found += 1
            hidden = is_junk(message.raw_payload)

            outcome = await dlr.apply(db, receipt, relay=relay) if apply else \
                await _preview(db, receipt)
            if outcome["applied"]:
                applied += 1
            elif outcome["reason"] == "already applied":
                already += 1
            else:
                orphan += 1

            if not hidden:
                marked += 1
                if apply:
                    raw = dict(message.raw_payload or {})
                    raw[DLR_JUNK_KEY] = {
                        "id": receipt.receipt_id, "stat": receipt.stat, "err": receipt.err,
                        "backfilled": True,
                        "uncorrelated": None if outcome["applied"] else outcome["reason"],
                    }
                    message.raw_payload = raw
                    # It is not a message, so it has no business being relayed onward
                    # either — and this stops any retry of an old relay job.
                    message.relayed_to_ghl = True
        if apply:
            await db.commit()

    print("stored delivery receipts found:        %d" % found)
    print("  applied to their outbound message:   %d" % applied)
    print("  already applied (nothing to do):     %d" % already)
    print("  could not be correlated:             %d" % orphan)
    print("hidden from threads%s:%s%d"
          % ("" if apply else " (would be)", " " * (18 if apply else 9), marked))
    if not apply:
        print("\nDRY RUN — nothing was written. Re-run with --apply.")
    return 0


async def _preview(db, receipt) -> dict:
    """What `dlr.apply` WOULD do, without writing. Uses the same correlation, so the dry
    run's counts are the counts `--apply` will produce."""
    found = await dlr.correlate(db, receipt)
    if not found.matched:
        return {"applied": False, "reason": found.how}
    if found.how == "already applied":
        return {"applied": False, "reason": "already applied"}
    return {"applied": True, "reason": found.how}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    ap.add_argument("--no-crm", action="store_true",
                    help="do not relay the receipts to the CRM")
    args = ap.parse_args()
    return asyncio.run(run(args.apply, not args.no_crm))


if __name__ == "__main__":
    sys.exit(main())

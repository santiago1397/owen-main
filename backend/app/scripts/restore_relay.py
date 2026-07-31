"""One-off: re-relay a parsed Dispatch email whose GHL records were destroyed.

The 2026-07-28 Workiz delete removed contact "Guillermo Escala" because the Workiz import
had touched it last, so its `source` read "Workiz Import" rather than "OWEN Email Ingest".
Deleting the contact cascaded to his relay opportunity (66450639 ROOF, $125, open).

OWEN still holds the parsed email, so the records are rebuildable from the original source
data. This clears the relayed flag and re-enqueues the normal relay job — no bespoke write
path, the same code that created the record the first time.

    python -m app.scripts.restore_relay <inbound_email_id> [...]
"""

import asyncio
import sys

from sqlalchemy import text

from app.db import SessionLocal
from app.services import queue


async def main() -> None:
    ids = sys.argv[1:]
    if not ids:
        sys.exit("usage: python -m app.scripts.restore_relay <inbound_email_id> [...]")

    async with SessionLocal() as db:
        for eid in ids:
            row = (await db.execute(text(
                "SELECT job_id, parse_status, relayed_to_ghl FROM inbound_emails WHERE id=:i"
            ), {"i": eid})).first()
            if not row:
                print(f"  {eid}: NOT FOUND")
                continue
            job_id, parse_status, relayed = row
            if parse_status != "parsed":
                print(f"  {eid}: parse_status={parse_status}, refusing to relay")
                continue

            # handle_email_relay_ghl short-circuits on relayed_to_ghl, so clear it first.
            await db.execute(text(
                "UPDATE inbound_emails SET relayed_to_ghl=false, relay_status=NULL, "
                "relay_error=NULL WHERE id=:i"
            ), {"i": eid})
            await db.commit()
            await queue.enqueue(db, "email_relay_ghl", {"email_id": eid})
            print(f"  {eid}: job_id={job_id} was_relayed={relayed} -> re-enqueued")

    print("done — the worker drains the queue on its normal cycle")


if __name__ == "__main__":
    asyncio.run(main())

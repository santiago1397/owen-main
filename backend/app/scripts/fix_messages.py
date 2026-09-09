"""One-off repair for inbound SMS stored before the BulkVS ingest fixes (2026-09-09).

Two defects, both fixed at the source; this repairs the rows already in the table:

1. BODY — BulkVS form-urlencodes the `Message` field inside its JSON, so bodies were stored
   as "Hi%2C+I+got+your+%23...". Re-decoded with the same guarded decoder the adapter now
   uses. `raw_payload` still holds the original, so this is recoverable either way.

2. ATTRIBUTION — the number lookup matched on provider_id alone, so a DID adopted from a
   legacy Twilio row (provider_id=twilio, owner_provider=bulkvs) never matched an SMS
   ingesting under `bulkvs`. Those messages have number_id/campaign_id NULL and show no
   "which number did they text?" in the Inbox. Re-resolved with the corrected rule.

Dry run by default; pass --apply to write.

    docker compose exec -T app python -m app.scripts.fix_messages
    docker compose exec -T app python -m app.scripts.fix_messages --apply
"""

import asyncio
import sys

from sqlalchemy import or_, select

from app.db import SessionLocal
from app.models import Message, Number, Provider
from app.providers.bulkvs import _decode_body


async def main(apply: bool) -> int:
    fixed_body = fixed_attr = 0
    async with SessionLocal() as db:
        providers = {
            p.id: p.name for p in (await db.execute(select(Provider))).scalars().all()
        }
        rows = (await db.execute(select(Message))).scalars().all()
        for m in rows:
            decoded = _decode_body(m.body)
            if decoded != m.body:
                print(f"  body  {m.id} {m.body[:48]!r}\n           -> {decoded[:48]!r}")
                if apply:
                    m.body = decoded
                fixed_body += 1

            if m.number_id is None and m.to_number:
                pname = providers.get(m.provider_id, "")
                num = (await db.execute(
                    select(Number).where(
                        Number.phone_number == m.to_number,
                        or_(
                            Number.provider_id == m.provider_id,
                            Number.owner_provider == pname,
                            Number.media_provider == pname,
                        ),
                    )
                )).scalars().first()
                if num is not None:
                    print(f"  attr  {m.id} to={m.to_number} -> {num.friendly_name!r} "
                          f"campaign={num.campaign_id}")
                    if apply:
                        m.number_id = num.id
                        if m.campaign_id is None:
                            m.campaign_id = num.campaign_id
                    fixed_attr += 1
                else:
                    print(f"  SKIP  {m.id} to={m.to_number} — no numbers row (provider {pname})")
        if apply:
            await db.commit()

    verb = "repaired" if apply else "would repair"
    print(f"\n{verb}: {fixed_body} body/bodies, {fixed_attr} attribution(s)")
    if not apply:
        print("dry run — pass --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--apply" in sys.argv)))

"""Minimal admin CLI for Phase 1 (before the Numbers UI exists in Phase 4).

Examples (inside the app container, or locally via the venv):
    python -m app.scripts.manage add-campaign --name "CL Ads 2" --source craigslist
    python -m app.scripts.manage add-number --phone +13055559999 --campaign "CL Ads 2" \
        --friendly "CL Ads 2" --forwards-to +13055550000
    python -m app.scripts.manage list
"""

import argparse
import asyncio

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import SessionLocal
from app.models import Call, Campaign, Number, Provider
from app.providers import signalwire_client


async def _provider(db, name: str) -> Provider:
    await db.execute(pg_insert(Provider).values(name=name).on_conflict_do_nothing(index_elements=["name"]))
    await db.commit()
    return (await db.execute(select(Provider).where(Provider.name == name))).scalar_one()


async def add_campaign(name: str, source: str | None) -> None:
    async with SessionLocal() as db:
        db.add(Campaign(name=name, source=source))
        await db.commit()
        print(f"campaign added: {name} ({source})")


async def add_number(phone: str, campaign: str, friendly: str | None,
                     forwards_to: str | None, provider: str) -> None:
    async with SessionLocal() as db:
        prov = await _provider(db, provider)
        camp = (await db.execute(select(Campaign).where(Campaign.name == campaign))).scalar_one_or_none()
        if not camp:
            raise SystemExit(f"campaign not found: {campaign!r} (create it with add-campaign first)")
        db.add(Number(provider_id=prov.id, campaign_id=camp.id, phone_number=phone,
                      friendly_name=friendly, forwards_to=forwards_to, active=True))
        await db.commit()
        print(f"number added: {phone} -> campaign {campaign} (provider {provider})")


async def list_all() -> None:
    async with SessionLocal() as db:
        print("== campaigns ==")
        for c in (await db.execute(select(Campaign))).scalars():
            print(f"  {c.name}  source={c.source}  active={c.active}")
        print("== numbers ==")
        for n in (await db.execute(select(Number))).scalars():
            print(f"  {n.phone_number}  friendly={n.friendly_name}  campaign_id={n.campaign_id}  active={n.active}")
        total = len((await db.execute(select(Call.id))).all())
        print(f"== calls: {total} ==")


async def sync_numbers(provider: str, dry_run: bool) -> None:
    """Pull the account's number inventory from SignalWire and upsert into `numbers`.

    Inserts numbers we don't have yet and refreshes `friendly_name` from SignalWire
    (the source of truth). Leaves `campaign_id` and `forwards_to` untouched so manual
    assignments survive re-runs. Idempotent — safe to run repeatedly.

    With dry_run, prints the inventory SignalWire returns and writes nothing."""
    inventory = await signalwire_client.fetch_incoming_phone_numbers()
    if dry_run:
        print(f"== signalwire numbers (dry-run): {len(inventory)} ==")
        for entry in inventory:
            print(f"  {entry.get('phone_number')}  friendly={entry.get('friendly_name')!r}  "
                  f"sid={entry.get('sid')}")
        print("(dry-run: nothing written)")
        return
    async with SessionLocal() as db:
        prov = await _provider(db, provider)
        inserted = updated = 0
        for entry in inventory:
            phone = entry.get("phone_number")
            if not phone:
                continue
            friendly = entry.get("friendly_name")
            existing = (
                await db.execute(
                    select(Number).where(
                        Number.provider_id == prov.id, Number.phone_number == phone
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(Number(provider_id=prov.id, phone_number=phone,
                              friendly_name=friendly, active=True))
                inserted += 1
            elif existing.friendly_name != friendly:
                existing.friendly_name = friendly
                updated += 1
        await db.commit()
        print(f"sync-numbers: {len(inventory)} from SignalWire, "
              f"{inserted} inserted, {updated} updated (provider {provider})")


async def list_sw_recordings(hours: int) -> None:
    """Read-only: what the SignalWire Recordings API returns for the last N hours.
    Use this to confirm Call Flow Builder recordings are actually exposed via the
    Compatibility API before relying on the poll."""
    recs = await signalwire_client.fetch_recent_recordings(hours)
    print(f"== signalwire recordings (last {hours}h): {len(recs)} ==")
    for r in recs:
        print(f"  sid={r.provider_recording_sid} call_sid={r.provider_call_sid} "
              f"status={r.status} dur={r.duration_seconds}s url={r.provider_url}")


async def list_sw_calls(hours: int) -> None:
    """Read-only: what the SignalWire Calls API returns for the last N hours (all legs)."""
    calls = await signalwire_client.fetch_recent_calls(hours)
    print(f"== signalwire calls (last {hours}h): {len(calls)} ==")
    for c in calls:
        print(f"  sid={c.provider_call_sid} to={c.to_number} from={c.from_number} "
              f"dir={c.direction} status={c.status}")


async def reconcile_now(hours: int | None) -> None:
    """Run the reconciler once, on demand — no need to wait for the 5-min schedule."""
    from app.workers.reconciler import reconcile_recent

    n = await reconcile_recent(hours)
    print(f"reconcile done: {n} inbound calls ingested")


def main() -> None:
    p = argparse.ArgumentParser(prog="manage")
    sub = p.add_subparsers(dest="cmd", required=True)

    lr = sub.add_parser("list-recordings", help="SignalWire Recordings API dump (read-only)")
    lr.add_argument("--hours", type=int, default=24)

    lc = sub.add_parser("list-calls", help="SignalWire Calls API dump (read-only)")
    lc.add_argument("--hours", type=int, default=24)

    rn = sub.add_parser("reconcile-now", help="Run the reconciler once immediately")
    rn.add_argument("--hours", type=int, default=None)

    sn = sub.add_parser("sync-numbers", help="Import the SignalWire number inventory into the DB")
    sn.add_argument("--provider", default="signalwire")
    sn.add_argument("--dry-run", action="store_true", help="Print the inventory, write nothing")

    c = sub.add_parser("add-campaign")
    c.add_argument("--name", required=True)
    c.add_argument("--source")

    n = sub.add_parser("add-number")
    n.add_argument("--phone", required=True, help="E.164, e.g. +13055559999")
    n.add_argument("--campaign", required=True, help="campaign name")
    n.add_argument("--friendly")
    n.add_argument("--forwards-to")
    n.add_argument("--provider", default="twilio")

    sub.add_parser("list")

    args = p.parse_args()
    if args.cmd == "add-campaign":
        asyncio.run(add_campaign(args.name, args.source))
    elif args.cmd == "add-number":
        asyncio.run(add_number(args.phone, args.campaign, args.friendly, args.forwards_to, args.provider))
    elif args.cmd == "list":
        asyncio.run(list_all())
    elif args.cmd == "list-recordings":
        asyncio.run(list_sw_recordings(args.hours))
    elif args.cmd == "list-calls":
        asyncio.run(list_sw_calls(args.hours))
    elif args.cmd == "reconcile-now":
        asyncio.run(reconcile_now(args.hours))
    elif args.cmd == "sync-numbers":
        asyncio.run(sync_numbers(args.provider, args.dry_run))


if __name__ == "__main__":
    main()

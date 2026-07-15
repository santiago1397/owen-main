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


def main() -> None:
    p = argparse.ArgumentParser(prog="manage")
    sub = p.add_subparsers(dest="cmd", required=True)

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


if __name__ == "__main__":
    main()

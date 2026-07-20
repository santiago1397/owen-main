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
from app.providers import signalwire_client, twilio_client
from app.services import queue
from app.services.ingestion import ingest_status_event
from app.services.recordings import ingest_recording_event

# Each provider exposes an identical `fetch_incoming_phone_numbers()` returning
# entries with `phone_number`/`friendly_name`/`sid`, so sync-numbers is provider-agnostic.
_NUMBER_SOURCES = {
    "signalwire": signalwire_client.fetch_incoming_phone_numbers,
    "twilio": twilio_client.fetch_incoming_phone_numbers,
}


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
    """Pull the account's number inventory from the provider and upsert into `numbers`.

    Inserts numbers we don't have yet and refreshes `friendly_name` from the provider
    (the source of truth). Leaves `campaign_id` and `forwards_to` untouched so manual
    assignments survive re-runs. Idempotent — safe to run repeatedly.

    With dry_run, prints the inventory the provider returns and writes nothing."""
    fetch = _NUMBER_SOURCES.get(provider)
    if fetch is None:
        raise SystemExit(f"unknown provider: {provider!r} (expected one of {sorted(_NUMBER_SOURCES)})")
    inventory = await fetch()
    if dry_run:
        print(f"== {provider} numbers (dry-run): {len(inventory)} ==")
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
        print(f"sync-numbers: {len(inventory)} from {provider}, "
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


_CALL_SOURCES = {
    "twilio": twilio_client.fetch_recent_calls,
    "signalwire": signalwire_client.fetch_recent_calls_voice_logs,
}
_RECORDING_SOURCES = {
    "twilio": twilio_client.fetch_recent_recordings,
    "signalwire": signalwire_client.fetch_recordings_via_voice_logs,
}


async def backfill(provider: str, hours: int, transcribe: bool) -> None:
    """One-time historical mirror of a provider's calls + recordings into OWEN.

    Non-destructive: never touches the provider-side copy (remote deletion is gated
    separately by DELETE_REMOTE_RECORDING and only runs inside recording_fetch). Unlike
    reconcile-now this does NOT enqueue GHL relays, so backfilling months of history
    won't flood the CRM with old calls.

    Recordings are downloaded by the normal recording_fetch worker. By default it passes
    skip_transcribe=True so this stays a pure audio+metadata copy with no OpenAI/LLM
    cost; leaving recordings un-transcribed also means retention never prunes them (the
    sweep only deletes transcribed audio). Pass --transcribe to run the full pipeline.

    `hours` is the look-back window; the default (~10y) captures the whole account."""
    from app.workers.reconciler import _is_inbound

    call_fetch = _CALL_SOURCES.get(provider)
    rec_fetch = _RECORDING_SOURCES.get(provider)
    if call_fetch is None or rec_fetch is None:
        raise SystemExit(f"unknown provider: {provider!r} (expected one of {sorted(_CALL_SOURCES)})")

    events = await call_fetch(hours)
    ingested = 0
    for evt in events:
        if not evt.provider_call_sid or not _is_inbound(evt):
            continue
        async with SessionLocal() as db:
            await ingest_status_event(db, provider, evt)
        ingested += 1
    print(f"backfill: {provider} calls — {ingested}/{len(events)} inbound ingested (window {hours}h)")

    recs = await rec_fetch(hours)
    enqueued = already = 0
    for rec in recs:
        if not rec.provider_recording_sid:
            continue
        async with SessionLocal() as db:
            row = await ingest_recording_event(db, provider, rec)
            if row.storage_path is None:
                await queue.enqueue(db, "recording_fetch", {
                    "provider": provider,
                    "recording_id": str(row.id),
                    "recording_sid": rec.provider_recording_sid,
                    "provider_url": rec.provider_url,
                    "skip_transcribe": not transcribe,
                })
                enqueued += 1
            else:
                already += 1
    print(f"backfill: {provider} recordings — {len(recs)} found, {enqueued} enqueued "
          f"for download, {already} already local (transcribe={transcribe})")


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

    bf = sub.add_parser("backfill", help="One-time historical mirror of a provider's "
                        "calls + recordings into OWEN (non-destructive, no GHL relay)")
    bf.add_argument("--provider", default="twilio", choices=["twilio", "signalwire"])
    bf.add_argument("--hours", type=int, default=87600, help="look-back window (default ~10y)")
    bf.add_argument("--transcribe", action="store_true",
                    help="also transcribe + analyze (default: raw audio only, no AI cost)")

    sn = sub.add_parser("sync-numbers", help="Import a provider's number inventory into the DB")
    sn.add_argument("--provider", default="signalwire", choices=["signalwire", "twilio"])
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
    elif args.cmd == "backfill":
        asyncio.run(backfill(args.provider, args.hours, args.transcribe))
    elif args.cmd == "sync-numbers":
        asyncio.run(sync_numbers(args.provider, args.dry_run))


if __name__ == "__main__":
    main()

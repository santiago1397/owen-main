"""Minimal admin CLI for Phase 1 (before the Numbers UI exists in Phase 4).

Examples (inside the app container, or locally via the venv):
    python -m app.scripts.manage add-campaign --name "CL Ads 2" --source craigslist
    python -m app.scripts.manage add-number --phone +13055559999 --campaign "CL Ads 2" \
        --friendly "CL Ads 2" --forwards-to +13055550000
    python -m app.scripts.manage list
"""

import argparse
import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.db import SessionLocal
from app.models import Call, Campaign, Number, Provider
from app.providers import signalwire_client, twilio_client
from app.services import queue
from app.services.ingestion import ingest_status_event
from app.services.recordings import ingest_recording_event

def _number_sources() -> dict:
    """provider name -> async fetch() returning number inventory entries with
    `phone_number`/`friendly_name`/`sid`. Every configured Twilio account is its own
    provider name (bound to its own creds), so sync-numbers imports each account's
    inventory under the matching provider identity. SignalWire stays a single provider."""
    sources = {
        acct.name: (lambda a=acct: twilio_client.fetch_incoming_phone_numbers(a))
        for acct in settings.twilio_accounts()
    }
    sources["signalwire"] = signalwire_client.fetch_incoming_phone_numbers
    return sources


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
    sources = _number_sources()
    fetch = sources.get(provider)
    if fetch is None:
        raise SystemExit(f"unknown provider: {provider!r} (expected one of {sorted(sources)})")
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
    from app.workers.reconciler import _call_sources, _is_inbound, _recording_sources

    call_sources, rec_sources = _call_sources(), _recording_sources()
    call_fetch = call_sources.get(provider)
    rec_fetch = rec_sources.get(provider)
    if call_fetch is None or rec_fetch is None:
        raise SystemExit(f"unknown provider: {provider!r} (expected one of {sorted(call_sources)})")

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


# --- Billing (BulkVS cost estimate) ---------------------------------------------------------
# Rates and manual adjustments are administered here rather than in the UI: the price sheet
# changes about once a year, and a rates editor is a lot of surface for that.


async def list_rates() -> None:
    from app.models import BillingRate

    async with SessionLocal() as db:
        rows = (
            await db.execute(select(BillingRate).order_by(BillingRate.unit, BillingRate.code))
        ).scalars().all()
    print(f"{'code':28} {'unit':11} {'amount':>10}  {'incr':>5}  src   label")
    for r in rows:
        incr = str(r.increment_seconds or "") if r.unit == "per_minute" else ""
        print(f"{r.code:28} {r.unit:11} {float(r.amount):>10.6f}  {incr:>5}  "
              f"{r.source:5} {r.label}")


async def set_rate(code: str, amount: float | None, increment: int | None,
                   minimum: int | None) -> None:
    """Correct a rate in the local price sheet.

    Only RECURRING charges (did.monthly.*, e911.monthly, did.setup) are computed from this
    table — per-call usage comes from BulkVS's own rated /voice records and is unaffected by
    anything set here.
    """
    from app.models import BillingRate

    async with SessionLocal() as db:
        row = await db.get(BillingRate, code)
        if row is None:
            raise SystemExit(f"no such rate code: {code!r} (see `billing-rates`)")
        if amount is not None:
            row.amount = amount
        if increment is not None:
            row.increment_seconds = increment
        if minimum is not None:
            row.minimum_seconds = minimum
        await db.commit()
        print(f"{code}: amount={float(row.amount):.6f} increment={row.increment_seconds} "
              f"minimum={row.minimum_seconds}")
    print("note: already-costed legs keep the rate they were billed at (history is frozen)")


async def add_adjustment(occurred_on: str, code: str, amount: float,
                         description: str | None) -> None:
    """Record an account-level charge with no call data behind it (LNP port fee, E911
    overage, LIDB update, directory listing)."""
    from datetime import date as _date

    from app.models import BillingAdjustment

    async with SessionLocal() as db:
        db.add(BillingAdjustment(
            occurred_on=_date.fromisoformat(occurred_on),
            code=code, amount=amount, description=description,
        ))
        await db.commit()
    print(f"adjustment added: {occurred_on} {code} ${amount:.4f}")


async def cost_now(hours: int | None) -> None:
    """Pull BulkVS's rated records on demand instead of waiting for the schedule."""
    from app.workers.billing import reconcile_charges

    n = await reconcile_charges(hours)
    print(f"billing reconcile done: {n} charge rows written")


async def purge_estimates() -> None:
    """One-time cleanup after switching from the Asterisk-CDR estimate to BulkVS's rated
    /voice feed: drops the bogus per-call CNAM rows (never actually billed) and the
    locally-priced minutes rows, which `cost-now` then re-creates from real amounts."""
    from app.workers.billing import purge_estimated_charges

    n = await purge_estimated_charges()
    print(f"purged {n} superseded estimated charge rows (run `cost-now` to repopulate)")


async def purge_dead_jobs(dry_run: bool, include_gone_upstream: bool) -> None:
    """Retire dead `recording_fetch` jobs whose media can never be fetched.

    A job that has exhausted its attempts against media the provider will not serve is not
    work waiting to happen — nothing will ever retry it, and it keeps /health/pipeline stuck
    on `degraded`, which drains the word of meaning.

    The failure is not discarded, it is MOVED to where it belongs: the recording row is marked
    `status='absent'` (the same marker the 404 path already uses) so "we know about this
    recording and its audio is gone" survives, and only the queue row is deleted. Deleting the
    job alone would have lost that.

    Strictly scoped to recording_fetch jobs that are dead AND whose error is a permanent
    upstream refusal (403/404) or an unusable URL. A dead job from any other cause is left
    alone — those are the ones a human should still see.
    """
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import or_

    from app.models import Job, Recording
    from app.services.queue import MAX_ATTEMPTS

    # By DEFAULT only the causes that leave /health/pipeline reporting `degraded`. A 404 means
    # the provider deleted the media on its own retention schedule; those are already counted
    # apart as `dead_media_gone_upstream` and are not a problem, so sweeping them is optional
    # housekeeping rather than something to do implicitly on a destructive command.
    causes = [
        Job.last_error.ilike("%403%"),                  # provider refuses it, with or without auth
        Job.last_error.ilike("%missing an%protocol%"),  # unusable URL (unexpanded CFB template)
    ]
    if include_gone_upstream:
        causes.append(Job.last_error.ilike("%404%"))
    permanent = or_(*causes)

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(Job).where(Job.type == "recording_fetch", Job.status == "failed",
                              Job.attempts >= MAX_ATTEMPTS, permanent)
        )).scalars().all()

        # Several dead jobs can point at ONE recording (the placeholder row was hit five
        # times), so track what we have already handled — otherwise the count reports
        # recordings that do not exist.
        seen: set = set()
        marked = 0
        for job in rows:
            rid = (job.payload or {}).get("recording_id")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            rec = await db.get(Recording, uuid.UUID(rid))
            # Only downgrade a recording we do NOT have locally; never relabel one on disk.
            if rec is not None and rec.storage_path is None and rec.status != "absent":
                print(f"  mark absent: recording {rec.id} sid={rec.provider_recording_sid!r}")
                if not dry_run:
                    rec.status = "absent"
                marked += 1

        if dry_run:
            await db.rollback()
            print(f"purge-dead-jobs (dry-run): {len(rows)} dead job(s) would be removed, "
                  f"{marked} recording(s) marked absent")
            return
        await db.execute(sa_delete(Job).where(Job.id.in_([j.id for j in rows])))
        await db.commit()
    print(f"purge-dead-jobs: removed {len(rows)} permanently-failed recording_fetch job(s); "
          f"marked {marked} recording(s) absent (their audio is unavailable upstream)")


async def purge_placeholder_recordings(dry_run: bool) -> None:
    """Delete recording rows that are not recordings.

    A SignalWire Call Flow Builder webhook can fire with its template variables unexpanded,
    producing a row whose provider_recording_sid is the literal string '%{call.recording.sid}'.
    That is a placeholder, not a call artifact: it can never be fetched, and because the sid is
    unique every such webhook collapses onto the same poisoned row.

    Refuses to touch anything with audio on disk or a transcript, so a real recording can never
    be caught by the pattern.
    """
    from sqlalchemy import delete as sa_delete

    from app.models import Recording, Transcription

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(Recording).where(Recording.provider_recording_sid.like("%\\%{%"))
        )).scalars().all()
        removable = []
        for rec in rows:
            has_transcript = (await db.execute(
                select(Transcription.id).where(Transcription.recording_id == rec.id).limit(1)
            )).scalar_one_or_none()
            if rec.storage_path is not None or has_transcript:
                print(f"  KEEP {rec.id} sid={rec.provider_recording_sid!r} — has audio/transcript")
                continue
            print(f"  delete placeholder: {rec.id} sid={rec.provider_recording_sid!r}")
            removable.append(rec.id)
        if dry_run:
            await db.rollback()
            print(f"purge-placeholder-recordings (dry-run): {len(removable)}/{len(rows)} would be deleted")
            return
        if removable:
            await db.execute(sa_delete(Recording).where(Recording.id.in_(removable)))
        await db.commit()
    print(f"purge-placeholder-recordings: deleted {len(removable)} placeholder row(s)")


async def reclassify_emails(dry_run: bool) -> None:
    """Re-derive `parse_status` for already-stored non-parsed emails.

    Rows written before the parser learned to tell these apart are all marked 'failed', which
    badly overstates the problem: on this account 4 of 5 were cancellations, notes and account
    mail. Re-running the classifier moves each to what it actually is —

        cancellation : the sender cancelled a job (actionable; relayed as a note)
        ignored      : not a work order at all (no lead, no action)
        failed       : a real work order we could not read (left alone, still loud)

    Classification needs only the SUBJECT, so this calls the real predicates rather than
    re-parsing bodies — no duplicated logic, and nothing can drift from what the poller does.
    Never touches a 'parsed' row, and never silences a genuine parse failure.

    Newly-identified cancellations get a relay job enqueued so they reach GoHighLevel like a
    freshly-received one would.
    """
    from app.models import InboundEmail
    from app.providers import dispatch_email

    async with SessionLocal() as db:
        rows = (await db.execute(
            select(InboundEmail).where(
                InboundEmail.parse_status.in_([dispatch_email.FAILED, dispatch_email.IGNORED])
            ).order_by(InboundEmail.received_at)
        )).scalars().all()

        changed, to_relay = 0, []
        for row in rows:
            cancelled = dispatch_email.cancelled_job_id(row.subject)
            if cancelled:
                want, why = dispatch_email.CANCELLATION, f"job {cancelled} was cancelled by the sender"
                fields = {**(row.fields or {}), "source": dispatch_email.SOURCE,
                          "kind": dispatch_email.CANCELLATION, "cancelled_job_id": cancelled}
            elif not dispatch_email.is_job_notification(row.subject):
                reason = dispatch_email.non_job_kind(row.subject)
                want, why, fields = (dispatch_email.IGNORED,
                                     f"ignored: {reason} — carries no lead to relay", row.fields)
            else:
                print(f"  KEEP failed  job={row.job_id} {row.subject!r}")
                continue

            if row.parse_status == want:
                continue
            print(f"  {row.parse_status} -> {want}  {row.subject!r}")
            changed += 1
            if not dry_run:
                row.parse_status = want
                row.parse_error = why
                row.fields = fields
                if want == dispatch_email.CANCELLATION:
                    row.job_id = cancelled
                    to_relay.append(row.id)

        if dry_run:
            await db.rollback()
            print(f"reclassify-emails (dry-run): {changed}/{len(rows)} would change")
            return
        await db.commit()
        for eid in to_relay:
            await queue.enqueue(db, "email_relay_ghl", {"email_id": str(eid)})
    print(f"reclassify-emails: {changed}/{len(rows)} reclassified; "
          f"{len(to_relay)} cancellation(s) enqueued for relay")


# --- AI API keys ----------------------------------------------------------------------------
# The UI (/api-keys) is the normal way to do this. These exist for bootstrapping — issuing the
# first key on a fresh deployment, or recovering when nobody can log in.


async def issue_key(name: str, scopes: list[str] | None, expires_days: int | None) -> None:
    from datetime import datetime, timedelta, timezone

    from app.core.apikeys import SCOPES, display_prefix, generate_key, hash_key, normalize_scopes
    from app.models import ApiKey

    unknown = [s for s in (scopes or []) if s.lower() not in SCOPES]
    if unknown:
        raise SystemExit(f"unknown scope(s): {', '.join(unknown)}. Valid: {', '.join(SCOPES)}")
    resolved = normalize_scopes(scopes)
    plaintext = generate_key()
    async with SessionLocal() as db:
        db.add(ApiKey(
            name=name, key_prefix=display_prefix(plaintext), key_hash=hash_key(plaintext),
            scopes=resolved,
            expires_at=(datetime.now(timezone.utc) + timedelta(days=expires_days)
                        if expires_days else None),
        ))
        await db.commit()
    print(f"key issued: {name}  scopes={','.join(resolved)}")
    print(f"\n  {plaintext}\n")
    print("This is the ONLY time the key is shown — it is stored hashed and cannot be recovered.")


async def list_keys() -> None:
    from app.models import ApiKey

    async with SessionLocal() as db:
        rows = (await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))).scalars().all()
    print(f"{'name':24} {'prefix':16} {'scopes':28} {'state':9} last used")
    for r in rows:
        state = "revoked" if r.revoked_at else ("active" if r.active else "inactive")
        used = r.last_used_at.strftime("%Y-%m-%d %H:%M") if r.last_used_at else "never"
        print(f"{r.name[:24]:24} {r.key_prefix:16} {','.join(r.scopes or [])[:28]:28} {state:9} {used}")


async def revoke_key(name: str) -> None:
    from datetime import datetime, timezone

    from app.models import ApiKey

    async with SessionLocal() as db:
        row = (await db.execute(select(ApiKey).where(ApiKey.name == name))).scalar_one_or_none()
        if row is None:
            raise SystemExit(f"no api key named {name!r} (see `list-keys`)")
        row.revoked_at = datetime.now(timezone.utc)
        row.active = False
        await db.commit()
    print(f"revoked: {name}")


def main() -> None:
    p = argparse.ArgumentParser(prog="manage")
    sub = p.add_subparsers(dest="cmd", required=True)

    # Valid provider names: every configured Twilio account (each its own provider
    # identity) plus signalwire. Lets --provider target a specific Twilio account.
    provider_choices = sorted({a.name for a in settings.twilio_accounts()} | {"signalwire"})
    default_twilio = next((a.name for a in settings.twilio_accounts()), "twilio")

    lr = sub.add_parser("list-recordings", help="SignalWire Recordings API dump (read-only)")
    lr.add_argument("--hours", type=int, default=24)

    lc = sub.add_parser("list-calls", help="SignalWire Calls API dump (read-only)")
    lc.add_argument("--hours", type=int, default=24)

    rn = sub.add_parser("reconcile-now", help="Run the reconciler once immediately")
    rn.add_argument("--hours", type=int, default=None)

    bf = sub.add_parser("backfill", help="One-time historical mirror of a provider's "
                        "calls + recordings into OWEN (non-destructive, no GHL relay)")
    bf.add_argument("--provider", default=default_twilio, choices=provider_choices)
    bf.add_argument("--hours", type=int, default=87600, help="look-back window (default ~10y)")
    bf.add_argument("--transcribe", action="store_true",
                    help="also transcribe + analyze (default: raw audio only, no AI cost)")

    sn = sub.add_parser("sync-numbers", help="Import a provider's number inventory into the DB")
    sn.add_argument("--provider", default="signalwire", choices=provider_choices)
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

    sub.add_parser("billing-rates", help="Show the configured BulkVS price sheet")

    sr = sub.add_parser("set-rate", help="Correct a rate/increment after checking an invoice")
    sr.add_argument("--code", required=True, help="e.g. outbound.domestic (see billing-rates)")
    sr.add_argument("--amount", type=float)
    sr.add_argument("--increment", type=int, help="billing increment in seconds (per-minute rates)")
    sr.add_argument("--minimum", type=int, help="minimum billed seconds (per-minute rates)")

    aa = sub.add_parser("add-adjustment", help="Record a manual account-level charge")
    aa.add_argument("--date", required=True, help="YYYY-MM-DD")
    aa.add_argument("--code", required=True, help="e.g. lnp / e911_overage / lidb / directory")
    aa.add_argument("--amount", type=float, required=True)
    aa.add_argument("--description")

    cn = sub.add_parser("cost-now", help="Pull BulkVS rated call records immediately")
    cn.add_argument("--hours", type=int)

    sub.add_parser("billing-purge-estimates",
                   help="One-time: drop charge rows written by the old local estimate")

    pdj = sub.add_parser("purge-dead-jobs",
                         help="Retire recording_fetch jobs whose media is permanently unfetchable")
    pdj.add_argument("--dry-run", action="store_true", help="Show what would change, write nothing")
    pdj.add_argument("--include-gone-upstream", action="store_true",
                     help="also sweep 404s (media the provider deleted; already benign)")

    ppr = sub.add_parser("purge-placeholder-recordings",
                         help="Delete recording rows created from unexpanded CFB template vars")
    ppr.add_argument("--dry-run", action="store_true", help="Show what would change, write nothing")

    re_ = sub.add_parser("reclassify-emails",
                         help="Re-file stored non-job 'failed' emails as 'ignored'")
    re_.add_argument("--dry-run", action="store_true", help="Show what would change, write nothing")

    ik = sub.add_parser("issue-key", help="Mint an AI API key (shown once). UI: /api-keys")
    ik.add_argument("--name", required=True, help="human label, e.g. 'claude-cli'")
    ik.add_argument("--scope", action="append", dest="scopes",
                    help="read | content | sql | logs (repeatable; default: read)")
    ik.add_argument("--expires-days", type=int)

    sub.add_parser("list-keys", help="Show issued AI API keys")

    rk = sub.add_parser("revoke-key", help="Revoke an AI API key by name")
    rk.add_argument("--name", required=True)

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
    elif args.cmd == "billing-rates":
        asyncio.run(list_rates())
    elif args.cmd == "set-rate":
        asyncio.run(set_rate(args.code, args.amount, args.increment, args.minimum))
    elif args.cmd == "add-adjustment":
        asyncio.run(add_adjustment(args.date, args.code, args.amount, args.description))
    elif args.cmd == "cost-now":
        asyncio.run(cost_now(args.hours))
    elif args.cmd == "billing-purge-estimates":
        asyncio.run(purge_estimates())
    elif args.cmd == "purge-dead-jobs":
        asyncio.run(purge_dead_jobs(args.dry_run, args.include_gone_upstream))
    elif args.cmd == "purge-placeholder-recordings":
        asyncio.run(purge_placeholder_recordings(args.dry_run))
    elif args.cmd == "reclassify-emails":
        asyncio.run(reclassify_emails(args.dry_run))
    elif args.cmd == "issue-key":
        asyncio.run(issue_key(args.name, args.scopes, args.expires_days))
    elif args.cmd == "list-keys":
        asyncio.run(list_keys())
    elif args.cmd == "revoke-key":
        asyncio.run(revoke_key(args.name))


if __name__ == "__main__":
    main()

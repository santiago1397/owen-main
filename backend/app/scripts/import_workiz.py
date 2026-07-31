"""Workiz CSV -> GoHighLevel importer (docs/WORKIZ_IMPORT.md).

First run 2026-07-24. REWRITTEN 2026-07-28 for the second run, after the first import's
records were deleted by `app.scripts.delete_workiz`.

    python -m app.scripts.validate_workiz_export /data/export.csv     # gate 1
    python -m app.scripts.delete_workiz                               # gate 2 (dry run)
    python -m app.scripts.import_workiz /data/export.csv --limit 10   # gate 3 (pilot)
    python -m app.scripts.import_workiz /data/export.csv              # remainder

Per spec W1/D21 there is no recurring Workiz sync; this is a bounded, manual exception and
OWEN otherwise never creates GHL records.

WHAT CHANGED IN THE 2026-07-28 REWRITE
  - Opportunity titles are now `{Client} - {Type}` — the customer's name leads. The Workiz
    job number has left the title entirely and lives only in `workiz_job_number`.
  - Because of that, the old idempotency trick (reading the job number back off the
    opportunity NAME) is gone. Three handles replace it: a ledger written to disk AS EACH
    ROW COMPLETES, a `workiz-import` tag on every contact, and the job number in a custom
    field. The 2026-07-24 run printed its ledger to stdout inside a `--rm` container and it
    was lost, which is why deleting that run's records was so awkward.
  - `owen_campaign` now uses the platform's real definition of a lead — `_qualified()` from
    app/api/attribution.py, first touch, no date window. The old query counted Twilio
    backfill stubs, robocalls and OUR OWN OUTBOUND DIALS as campaign evidence, and broke ties
    with `max(name)`, i.e. alphabetically.
  - Multi-job clients: `--duplicates sum` puts the client's whole won total on their single
    card instead of stranding $32k in notes. Use `--duplicates allow` if the location's
    duplicate-opportunity setting has been turned on, and every job gets its own card.
  - Appointments are booked for EVERY job, including ones that fall back to a note. W5 asked
    for that; the first run only booked them for jobs that got an opportunity.
  - `--dry-run` genuinely writes nothing now. It used to create custom fields before the
    dry-run check.

INHERITED GOTCHAS, all still load-bearing:
  - the CSV is UTF-8 (`utf-8-sig`), NOT cp1252 — reading it as cp1252 corrupts the en-dash in
    "AHS – Repair Scheduled" and the import will happily report 10/10 OK while doing it;
  - Workiz's `End` is a job-CLOSURE timestamp (71% of rows are >7 days after `Scheduled`,
    worst 310 days), so appointments book a fixed 2h slot from `Scheduled` instead;
  - `/opportunities/search` returns customFields WITHOUT values, so nothing here may rely on
    reading a custom field back out of a search result.

Run it from a throwaway container on the server so it has both the GHL token and the OWEN
database in one process (mount a volume for the ledger — do NOT let it die with the container
this time):

    docker compose -f docker-compose.prod.yml --env-file .env.prod \
        run --rm --no-deps -v /opt/santiagoproperties/owen-main/data:/data \
        app python -m app.scripts.import_workiz /data/export.csv
"""

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

# _qualified is imported rather than re-implemented on purpose: there must be exactly ONE
# definition of "a call that counts as a lead", and it is the one the /campaigns page uses.
# If that rule changes, this import must move with it.
from app.api.attribution import DEFAULT_MIN_DURATION, _qualified
from app.core.config import settings
from app.db import SessionLocal
from app.models import Call, Caller, Campaign

TZ = ZoneInfo("America/New_York")
FMT = "%a %b %d, %Y %I:%M %p"
APPT_HOURS = 2

# "Workiz Jobs (imported)", recreated 2026-07-29 after delete_workiz removed the original
# (hRPMITl1zpCZQnCByxwV) along with its 267 events. Override with --calendar-id or
# WORKIZ_CALENDAR_ID; a blank value skips appointments entirely.
CALENDAR_ID = os.getenv("WORKIZ_CALENDAR_ID", "zfaFyQE9tCHasRFjH8DM")
PIPE_AHS = "TRbZj4CJ88qZJqr1TRGA"          # Dream Team Roofing AHS
PIPE_RETAIL = "FwTahs2XI0w7D98LQOEk"       # Dream Team Roofing Retail Repairs

# Tag stamped on every contact this import touches. Unlike `source`, tags MERGE on upsert, so
# the email relay cannot overwrite it — this is the handle for the next cleanup.
IMPORT_TAG = "workiz-import"

WORKIZ_FIELDS = [
    "workiz_job_number", "workiz_job_name", "workiz_type", "workiz_status",
    "workiz_source", "workiz_tech", "workiz_created_by", "workiz_scheduled",
    "workiz_end", "workiz_job_created", "workiz_tags", "workiz_total",
    "attribution_basis",
]
# Created by the GHL-sync work (spec D15), NOT by this script. Populated when present.
EXTERNAL_FIELDS = ["owen_campaign"]

# Workiz status -> (GHL status, AHS stage, Retail stage). See spec W4.
# `done pending approval` is deliberately OPEN, not won: AHS has not approved it, so banking
# it overstates revenue. frontend/src/lib/workizJobs.ts was aligned to this on 2026-07-28.
STATUS_MAP = {
    "Done":                          ("won",  "Submit The Invoice",         "Closed"),
    "Canceled":                      ("lost", "New Lead",                   "Closed"),
    "done pending approval":         ("open", "Request the Approval (AHS)", "Proposal Sent"),
    "Pending (Estimate Follow Up)":  ("open", "New Lead",                   "Contacted"),
    "In Progress (Inspections)":     ("open", "Inspection",                 "Contacted"),
    "In Progress (Repair Schedule)": ("open", "Approved- Repair Schedule",  "Proposal Sent"),
    "Pending (New Roof Estimate)":   ("open", "New Lead",                   "Proposal Sent"),
    "Submitted":                     ("open", "Submit The Invoice",         "Proposal Sent"),
    "In Progress (Callback)":        ("open", "Call Back",                  "Contacted"),
    "Pending (Collect Balance)":     ("open", "Submit The Invoice",         "Closed"),
    # Added 2026-07-28: appeared in the new export as "In Progress (Request Approval  )",
    # with two spaces before the paren. Mapped alongside `done pending approval` — it is the
    # same AHS approval gate, just not finished yet.
    "In Progress (Request Approval)": ("open", "Request the Approval (AHS)", "Proposal Sent"),
}


def norm_status(s: str) -> str:
    """Workiz's status strings carry inconsistent internal whitespace.

    The new export writes "In Progress (Request Approval  )" with two spaces before the
    paren; an exact-match lookup misses it and the job silently lands in open/New Lead with
    no error. Normalising both sides means Workiz can drift its spacing without quietly
    mis-staging jobs.
    """
    return re.sub(r"\s+", " ", (s or "").strip()).replace(" )", ")").replace("( ", "(")


STATUS_LOOKUP = {norm_status(k): v for k, v in STATUS_MAP.items()}
WON_STATUSES = {k for k, (g, *_) in STATUS_LOOKUP.items() if g == "won"}

# Workiz `Source` values that are genuine acquisition-channel claims. Anything else (AHS,
# "Existig Customer", AI) is not a channel claim, so OWEN finding a campaign ENRICHES rather
# than contradicts it — that distinction is what `attribution_basis` records.
# Mirrored by SOURCE_TO_CAMPAIGN in frontend/src/lib/workizJobs.ts — keep the two in step.
CHANNEL_SRC = {"Google": "GBP", "CL- ADS": "Craiglist", "FB": "Facebook"}

# Placeholder numbers in the export. They match nothing and would otherwise be written onto a
# contact as if real. Mirrors JUNK_PHONES in frontend/src/lib/workizJobs.ts.
JUNK_PHONES = {"+11111111111", "+10000000000", "+11234567890"}


def hdr() -> dict:
    return {"Authorization": f"Bearer {settings.GHL_API_TOKEN}",
            "Version": settings.GHL_API_VERSION,
            "Accept": "application/json", "Content-Type": "application/json"}


def g(row: dict, key: str) -> str:
    return (row.get(key) or "").strip()


def money(row: dict) -> float:
    try:
        return float(g(row, "Total").replace(",", "").replace("$", ""))
    except ValueError:
        return 0.0


def e164(phone: str):
    d = re.sub(r"\D", "", phone or "")
    e = "+1" + d if len(d) == 10 else ("+" + d if len(d) == 11 and d.startswith("1") else None)
    return e if e and e not in JUNK_PHONES else None


def appt_window(scheduled: str):
    """(startISO, endISO). Workiz `End` is a closure timestamp, so it is NEVER used here."""
    try:
        start = datetime.strptime(scheduled, FMT).replace(tzinfo=TZ)
    except ValueError:
        return None, None
    return start.isoformat(), (start + timedelta(hours=APPT_HOURS)).isoformat()


def split_name(full: str):
    parts = (full or "").split()
    return (parts[0], " ".join(parts[1:])) if parts else ("", "")


def title_for(row: dict) -> str:
    """`{Client} - {Type}` — the customer's name leads (agreed 2026-07-28).

    The Workiz job number is deliberately absent; it lives in workiz_job_number. Nothing in
    this codebase may parse a job number back out of an opportunity name.
    """
    return " - ".join(x for x in [g(row, "Client"), g(row, "Type")] if x)[:100] or "Workiz job"


async def ensure_fields(c, create: bool) -> dict:
    """name -> field id for every opportunity custom field we use.

    `create=False` (dry run) only reads, so a dry run cannot mutate the account. External
    fields such as owen_campaign are looked up but never created here — spec D15 owns them,
    and if they are absent the values are simply omitted rather than silently invented.
    """
    r = await c.get(f"{settings.GHL_API_BASE}/locations/{settings.GHL_LOCATION_ID}/customFields",
                    params={"model": "opportunity"}, headers=hdr())
    have = {str(f.get("name", "")).lower(): f.get("id")
            for f in (r.json().get("customFields") or [])} if r.status_code == 200 else {}
    missing_external = [n for n in EXTERNAL_FIELDS if n.lower() not in have]
    if missing_external:
        print(f"  NOTE: {missing_external} do not exist in this location (spec D15 creates "
              f"them). Those values will be omitted from every card.")
    if not create:
        absent = [n for n in WORKIZ_FIELDS if n.lower() not in have]
        if absent:
            print(f"  [DRY] would create {len(absent)} custom fields: {absent}")
        return have
    for name in WORKIZ_FIELDS:
        if name.lower() in have:
            continue
        rr = await c.post(
            f"{settings.GHL_API_BASE}/locations/{settings.GHL_LOCATION_ID}/customFields",
            json={"name": name, "dataType": "TEXT", "model": "opportunity"}, headers=hdr())
        if rr.status_code in (200, 201):
            have[name.lower()] = (rr.json().get("customField") or rr.json()).get("id")
        else:
            print(f"  custom field {name}: HTTP {rr.status_code} {rr.text[:120]}")
    return have


async def stage_ids(c) -> dict:
    r = await c.get(f"{settings.GHL_API_BASE}/opportunities/pipelines",
                    params={"locationId": settings.GHL_LOCATION_ID}, headers=hdr())
    return {p["id"]: {s["name"]: s["id"] for s in (p.get("stages") or [])}
            for p in r.json().get("pipelines", [])}


async def owen_campaigns(phones: list, min_seconds: int) -> dict:
    """phone -> the campaign whose tracking number the caller FIRST dialled.

    Uses `_qualified()` from app/api/attribution.py verbatim, so a job's campaign on a GHL
    card and the same job's campaign on the /campaigns page can never disagree. That means:
    inbound only, non-NULL started_at (excluding ~25k Twilio backfill stubs), duration above
    the floor, and a campaign actually attached.

    Deliberately UNWINDOWED, unlike the API which defaults to a trailing year: these jobs run
    back to Mar 2025 and the call that produced one may be older still.
    """
    if not phones:
        return {}
    # A window wide enough to be no window at all — _qualified() takes one by construction.
    start = datetime(1970, 1, 1, tzinfo=timezone.utc)
    end = datetime(2100, 1, 1, tzinfo=timezone.utc)
    where = _qualified(start, end, min_seconds) + [
        Caller.phone_number.in_(phones),
        Call.campaign_id.is_not(None),
    ]
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(Caller.phone_number, Campaign.name)
            .join(Call, Call.caller_id == Caller.id)
            .join(Campaign, Call.campaign_id == Campaign.id)
            .where(*where)
            # DISTINCT ON + ascending start = first touch, matching attribution._edge(True).
            .distinct(Caller.phone_number)
            .order_by(Caller.phone_number, Call.started_at.asc())
        )).all()
    return {phone: camp for phone, camp in rows if camp}


def attribution_basis(src: str, owen_camp) -> str:
    if not owen_camp:
        return "workiz-only"
    if src not in CHANNEL_SRC:
        return "enriched"
    return "call-verified" if CHANNEL_SRC[src].lower() == owen_camp.lower() else "conflict"


def load_ledger(path: str) -> dict:
    """job number -> ledger entry, for resuming a run that died partway.

    JSON Lines, appended per row, so a killed process loses at most the row in flight. The
    2026-07-24 run's single stdout dump is exactly what we are avoiding here.
    """
    if not path or not os.path.exists(path):
        return {}
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue          # a torn final line from a hard kill; ignore it
            if entry.get("job"):
                out[entry["job"]] = entry
    return out


async def import_rows(rows: list, *, dry_run: bool, duplicates: str, ledger_path: str,
                      calendar_id: str, min_seconds: int) -> list:
    loc = settings.GHL_LOCATION_ID
    base = settings.GHL_API_BASE
    written = []

    done_before = load_ledger(ledger_path)
    if done_before:
        print(f"resuming: {len(done_before)} job(s) already in {ledger_path}")

    # With duplicates=sum, a client's single card carries the sum of their WON jobs, so the
    # pipeline stops under-reporting revenue by the value of every 2nd+ job (the 2026-07-24
    # run stranded $32,425 this way). Computed up front from the CSV, not from GHL.
    # Keyed on (phone, PIPELINE), not phone alone. GHL permits one opportunity per contact
    # PER PIPELINE — verified 2026-07-28, when 6 contacts were found holding two cards each,
    # every pair straddling AHS and Retail. So a client with both AHS and Retail work gets a
    # card in each, and the sums must not be pooled across them.
    won_by_key = defaultdict(float)
    for r in rows:
        p = e164(g(r, "Phone"))
        if p and norm_status(g(r, "Status")) in WON_STATUSES:
            won_by_key[(p, PIPE_AHS if g(r, "Source") == "AHS" else PIPE_RETAIL)] += money(r)

    async with httpx.AsyncClient(timeout=45) as c:
        fields = await ensure_fields(c, create=not dry_run)
        stages = await stage_ids(c)
        phones = sorted({p for p in (e164(g(r, "Phone")) for r in rows) if p})
        camps = await owen_campaigns(phones, min_seconds)
        print(f"fields={len(fields)} phones={len(phones)} owen_matches={len(camps)} "
              f"duplicates={duplicates} calendar={calendar_id or '(none — no appointments)'}")

        contacts_seen = set()

        for r in rows:
            job = g(r, "Job #")
            if job in done_before:
                print(f"  [SKIP] {job}  already in ledger")
                continue

            phone = e164(g(r, "Phone"))
            src = g(r, "Source")
            is_ahs = src == "AHS"
            pipe = PIPE_AHS if is_ahs else PIPE_RETAIL
            wstat = g(r, "Status")
            gstatus, ahs_stage, ret_stage = STATUS_LOOKUP.get(
                norm_status(wstat), ("open", "New Lead", "New Lead"))
            stage = stages.get(pipe, {}).get(ahs_stage if is_ahs else ret_stage)
            owen_camp = camps.get(phone)
            basis = attribution_basis(src, owen_camp)
            title = title_for(r)

            # First card for this contact carries the summed won revenue; later ones are the
            # duplicates GHL will reject anyway.
            pipe_key = (phone, pipe)
            first_for_contact = pipe_key not in contacts_seen
            value = money(r)
            if duplicates == "sum" and phone and first_for_contact:
                value = won_by_key.get(pipe_key, 0.0) or money(r)

            if dry_run:
                print(f"  [DRY] {job:<8} {gstatus:<5} ${value:>9,.0f} basis={basis:<13} {title}")
                if phone:
                    contacts_seen.add(pipe_key)
                continue

            first, last = split_name(g(r, "Client"))
            tags = [t.strip() for t in g(r, "Tags").split(",") if t.strip()]
            cbody = {"locationId": loc, "firstName": first, "lastName": last,
                     "name": g(r, "Client") or phone or "Workiz contact",
                     "source": "Workiz Import",
                     # IMPORT_TAG last so it is unmistakable in the GHL UI. Tags merge on
                     # upsert; `source` does not, which is why the tag is the real handle.
                     "tags": tags + [IMPORT_TAG]}
            if phone:
                cbody["phone"] = phone
            if g(r, "Email"):
                cbody["email"] = g(r, "Email")
            for k, col in (("address1", "Address"), ("city", "City"),
                           ("state", "State"), ("postalCode", "Zip code")):
                if g(r, col):
                    cbody[k] = g(r, col)

            # /contacts/upsert dedupes on phone or email and 400s when the row has NEITHER —
            # e.g. job 25TNEL, whose Workiz phone is the placeholder 1111111111 (rejected by
            # e164) with no email, but which is a real $100 Done job. Fall back to a plain
            # create so a Workiz data-entry defect does not silently drop revenue. Safe
            # against duplicates because the ledger stops the row being attempted twice.
            endpoint = "contacts/upsert" if (phone or g(r, "Email")) else "contacts/"
            rc = await c.post(f"{base}/{endpoint}", json=cbody, headers=hdr())
            if rc.status_code not in (200, 201):
                print(f"  [FAIL] {job} contact ({endpoint}) {rc.status_code}: {rc.text[:140]}")
                continue
            cid = (rc.json().get("contact") or {}).get("id")
            if phone:
                contacts_seen.add(pipe_key)

            vals = {"workiz_job_number": job, "workiz_job_name": g(r, "Job name"),
                    "workiz_type": g(r, "Type"), "workiz_status": wstat,
                    "workiz_source": src, "workiz_tech": g(r, "Tech"),
                    "workiz_created_by": g(r, "Created by"),
                    "workiz_scheduled": g(r, "Scheduled"), "workiz_end": g(r, "End"),
                    "workiz_job_created": g(r, "Job Created"), "workiz_tags": g(r, "Tags"),
                    "workiz_total": g(r, "Total"), "attribution_basis": basis}
            if owen_camp:
                vals["owen_campaign"] = owen_camp
            cf = [{"id": fields[k.lower()], "field_value": v}
                  for k, v in vals.items() if fields.get(k.lower()) and v]

            # In `sum` mode the client's whole won total is already on their FIRST card, so a
            # second card for the same contact would double-count. Don't even attempt it —
            # relying on GHL to reject the duplicate only works while the location's
            # duplicate-opportunity setting is off, and silently double-counts if it is on.
            skip_opportunity = duplicates == "sum" and phone and not first_for_contact
            ro = None
            if not skip_opportunity:
                obody = {"pipelineId": pipe, "locationId": loc, "name": title,
                         "status": gstatus, "contactId": cid,
                         "monetaryValue": value, "customFields": cf}
                if stage:
                    obody["pipelineStageId"] = stage
                ro = await c.post(f"{base}/opportunities/", json=obody, headers=hdr())

            oid, noted = None, False
            if ro is not None and ro.status_code in (200, 201):
                oid = (ro.json().get("opportunity") or {}).get("id")
            elif skip_opportunity or "OPPORTUNITY_NO_DUPLICATE" in ((ro.text if ro else "") or ""):
                # Either we chose not to create a second card (duplicates=sum, money already
                # on the client's first card), or GHL rejected it. Either way the job becomes
                # a note so nothing from the export is lost.
                why = ("its value is included on this contact's card" if skip_opportunity
                       else "GHL allows one opportunity per contact")
                note = chr(10).join([
                    f"Workiz job {job} (additional job — {why})",
                    f"Type: {g(r, 'Type')}", f"Status: {wstat} -> {gstatus}",
                    f"Scheduled: {g(r, 'Scheduled')}   End: {g(r, 'End')}",
                    f"Total: {g(r, 'Total')}",
                    f"Source: {src}" + (f"   |   OWEN campaign: {owen_camp}"
                                        if owen_camp else ""),
                    f"Tech: {g(r, 'Tech')}", f"Tags: {g(r, 'Tags')}",
                ])
                rn = await c.post(f"{base}/contacts/{cid}/notes",
                                  json={"body": note}, headers=hdr())
                noted = rn.status_code in (200, 201)
                if not noted:
                    print(f"  [FAIL] {job} note {rn.status_code}: {rn.text[:140]}")
            else:
                print(f"  [FAIL] {job} opportunity {ro.status_code}: {ro.text[:140]}")
                continue

            # W5: every job gets an appointment, including the note path. The 2026-07-24 run
            # skipped these for note-path jobs, so 31 jobs had no calendar presence at all.
            appt = None
            st, en = appt_window(g(r, "Scheduled"))
            if calendar_id and st and en and cid:
                ra = await c.post(f"{base}/calendars/events/appointments", headers=hdr(),
                                  json={"calendarId": calendar_id, "locationId": loc,
                                        "contactId": cid, "startTime": st, "endTime": en,
                                        "title": title, "appointmentStatus": "confirmed",
                                        "ignoreFreeSlotValidation": True, "toNotify": False})
                if ra.status_code in (200, 201):
                    appt = ra.json().get("id")
                else:
                    print(f"  [WARN] {job} appointment {ra.status_code}: {ra.text[:120]}")

            # Revenue that reached NO card. Counted only for the job carrying the summed
            # value (the first for its contact+pipeline); later jobs' money is already inside
            # that sum. This happens when the contact already holds a non-Workiz opportunity
            # in the same pipeline — e.g. a Dispatch relay card — so GHL rejects ours. Run 1
            # stranded $32,425 this way and nobody noticed until the CSV was re-analysed by
            # hand; it is reported at the end of every run now.
            entry = {"job": job, "contact": cid, "opportunity": oid, "appointment": appt,
                     "note": noted, "title": title, "value": value, "basis": basis,
                     "stranded": value if (noted and not skip_opportunity) else 0.0}
            written.append(entry)
            # Appended immediately: a kill after this line loses nothing.
            if ledger_path:
                with open(ledger_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + chr(10))
            kind = "NOTE" if noted else "OK"
            print(f"  [{kind:<4}] {job:<8} {gstatus:<5} ${value:>9,.0f} "
                  f"basis={basis:<13} {title}")
    return written


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv_path")
    ap.add_argument("--limit", type=int, default=0,
                    help="import at most N rows (from the top; already-imported rows are "
                         "skipped via the ledger, so batch 2 is --limit 200, not --limit 100)")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan only — reads GHL and the database, writes nothing anywhere")
    ap.add_argument("--duplicates", choices=("sum", "allow"), default="sum",
                    help="sum: one card per contact carrying their whole won total (default). "
                         "allow: one card per job — only correct if the location's "
                         "duplicate-opportunity setting is ON.")
    ap.add_argument("--ledger", default="/data/workiz_ledger.jsonl",
                    help="append-per-row ledger; also the resume source")
    ap.add_argument("--calendar-id", default=CALENDAR_ID,
                    help="calendar for appointments; blank skips appointments entirely")
    ap.add_argument("--min-duration", type=int, default=DEFAULT_MIN_DURATION,
                    help="duration floor for a call to count as campaign evidence")
    args = ap.parse_args()

    if not settings.ghl_api_enabled:
        sys.exit("GHL_API_TOKEN / GHL_LOCATION_ID not configured")

    with open(args.csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows)} row(s) from {args.csv_path}")
    if not args.calendar_id and not args.dry_run:
        print("WARNING: no calendar id — appointments will NOT be booked. Pass --calendar-id "
              "or set WORKIZ_CALENDAR_ID.")

    written = await import_rows(
        rows, dry_run=args.dry_run, duplicates=args.duplicates, ledger_path=args.ledger,
        calendar_id=args.calendar_id, min_seconds=args.min_duration)

    opps = sum(1 for e in written if e["opportunity"])
    notes = sum(1 for e in written if e["note"])
    appts = sum(1 for e in written if e["appointment"])
    won = sum(e["value"] for e in written if e["opportunity"])
    print(f"\nwritten: {len(written)}   opportunities {opps} · notes {notes} · "
          f"appointments {appts}")
    print(f"value on cards: ${won:,.0f}")

    # Revenue that reached no card at all. Run 1 stranded $32,425 this way and nobody knew
    # until the CSV was re-analysed by hand months later — so it is reported every run, never
    # left for someone to discover.
    blocked = [e for e in written if (e.get("stranded") or 0) > 0]
    if blocked:
        total = sum(e["stranded"] for e in blocked)
        print(f"\n*** ${total:,.0f} of value reached NO card, across {len(blocked)} job(s). "
              f"GHL permits one\n    opportunity per contact per pipeline and the contact "
              f"already held one, so the job\n    is recorded as a note instead:")
        for e in sorted(blocked, key=lambda x: -x["stranded"]):
            print(f"      {e['job']:<8} ${e['stranded']:>9,.0f}  {e['title']}")
        print("    Fix by hand in GHL where the money matters, or accept the note as the "
              "record.")

    if not args.dry_run:
        print(f"ledger: {args.ledger}   <-- KEEP THIS FILE. It is how the next cleanup "
              f"finds these records.")


if __name__ == "__main__":
    asyncio.run(main())

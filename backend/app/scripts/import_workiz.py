"""One-off Workiz CSV -> GoHighLevel importer (docs/WORKIZ_IMPORT.md).

Executed once on 2026-07-24 against the live account. Kept in the repo so the import is
reproducible and auditable, NOT because it runs on a schedule — per spec W1/D21 there is no
recurring Workiz sync, and OWEN otherwise never creates GHL records.

    python -m app.scripts.import_workiz /path/to/export.csv [--limit N] [--dry-run]

Behaviour and every gotcha it works around are documented in docs/WORKIZ_IMPORT.md. The
short version:

  - the CSV is UTF-8 (`utf-8-sig`), NOT cp1252 — reading it as cp1252 corrupts the en-dash
    in "AHS – Repair Scheduled", which is the most common tag in the file;
  - Workiz's `End` is a job-CLOSURE timestamp (71% of rows are >7 days after `Scheduled`,
    worst 310 days), so appointments book a fixed 2h slot from `Scheduled` instead;
  - GHL permits ONE opportunity per contact, so a multi-job client's 2nd+ job is recorded
    as a NOTE rather than being lost;
  - `/opportunities/search` returns customFields WITHOUT values, so already-imported jobs are
    detected by the opportunity NAME prefix (`{Job #} - `), never by a custom field.

Run it from a throwaway container on the server so it has both the GHL token and the OWEN
database in one process:

    docker compose -f docker-compose.prod.yml --env-file .env.prod \
        run --rm --no-deps app python -m app.scripts.import_workiz /data/export.csv
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text

from app.core.config import settings
from app.db import SessionLocal

TZ = ZoneInfo("America/New_York")
FMT = "%a %b %d, %Y %I:%M %p"
APPT_HOURS = 2

# Created during the 2026-07-24 run; reused on any re-run.
CALENDAR_ID = "hRPMITl1zpCZQnCByxwV"      # "Workiz Jobs (imported)"
PIPE_AHS = "TRbZj4CJ88qZJqr1TRGA"          # Dream Team Roofing AHS
PIPE_RETAIL = "FwTahs2XI0w7D98LQOEk"       # Dream Team Roofing Retail Repairs

WORKIZ_FIELDS = [
    "workiz_job_number", "workiz_job_name", "workiz_type", "workiz_status",
    "workiz_source", "workiz_tech", "workiz_created_by", "workiz_scheduled",
    "workiz_end", "workiz_job_created", "workiz_tags", "workiz_total",
    "attribution_basis",
]

# Workiz status -> (GHL status, AHS stage, Retail stage). See spec W4.
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
}

# Workiz `Source` values that are genuine acquisition-channel claims. Anything else (AHS,
# "Existig Customer", AI) is not a channel claim, so OWEN finding a campaign ENRICHES rather
# than contradicts it — that distinction is what `attribution_basis` records.
CHANNEL_SRC = {"Google": "GBP", "CL- ADS": "Craiglist", "FB": "Facebook"}


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
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return None


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


async def ensure_fields(c) -> dict:
    r = await c.get(f"{settings.GHL_API_BASE}/locations/{settings.GHL_LOCATION_ID}/customFields",
                    params={"model": "opportunity"}, headers=hdr())
    have = {str(f.get("name", "")).lower(): f.get("id")
            for f in (r.json().get("customFields") or [])} if r.status_code == 200 else {}
    for name in WORKIZ_FIELDS:
        if name.lower() in have:
            continue
        rr = await c.post(
            f"{settings.GHL_API_BASE}/locations/{settings.GHL_LOCATION_ID}/customFields",
            json={"name": name, "dataType": "TEXT", "model": "opportunity"}, headers=hdr())
        if rr.status_code in (200, 201):
            have[name.lower()] = (rr.json().get("customField") or rr.json()).get("id")
    return have


async def stage_ids(c) -> dict:
    r = await c.get(f"{settings.GHL_API_BASE}/opportunities/pipelines",
                    params={"locationId": settings.GHL_LOCATION_ID}, headers=hdr())
    return {p["id"]: {s["name"]: s["id"] for s in (p.get("stages") or [])}
            for p in r.json().get("pipelines", [])}


async def imported_job_numbers(c) -> set:
    """Job numbers already in GHL, read from the opportunity NAME prefix.

    Deliberately NOT read from customFields: `/opportunities/search` returns customFields
    with no values, so a field-based check silently matches nothing and the import
    re-attempts everything (see docs/WORKIZ_IMPORT.md)."""
    seen, page = set(), 1
    while page <= 20:
        r = await c.get(f"{settings.GHL_API_BASE}/opportunities/search",
                        params={"location_id": settings.GHL_LOCATION_ID,
                                "limit": 100, "page": page}, headers=hdr())
        if r.status_code != 200:
            break
        opps = r.json().get("opportunities", [])
        if not opps:
            break
        for o in opps:
            m = re.match(r"^([A-Z0-9]{6})\s-\s", o.get("name") or "")
            if m:
                seen.add(m.group(1))
        if len(opps) < 100:
            break
        page += 1
    return seen


async def owen_campaigns(phones: list) -> dict:
    """phone -> OWEN campaign name, from the tracking number the caller actually dialled."""
    if not phones:
        return {}
    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT cl.phone_number, max(cp.name)
            FROM callers cl
            JOIN calls c ON c.caller_id = cl.id AND c.number_id IS NOT NULL
            LEFT JOIN campaigns cp ON cp.id = c.campaign_id
            WHERE cl.phone_number = ANY(:ph)
            GROUP BY cl.phone_number
        """), {"ph": phones})).all()
    return {p: camp for p, camp in rows if camp}


def attribution_basis(src: str, owen_camp) -> str:
    if not owen_camp:
        return "workiz-only"
    if src not in CHANNEL_SRC:
        return "enriched"
    return "call-verified" if CHANNEL_SRC[src].lower() == owen_camp.lower() else "conflict"


async def import_rows(rows: list, dry_run: bool = False) -> list:
    loc = settings.GHL_LOCATION_ID
    base = settings.GHL_API_BASE
    ledger = []
    async with httpx.AsyncClient(timeout=45) as c:
        fields = await ensure_fields(c)
        stages = await stage_ids(c)
        camps = await owen_campaigns([p for p in (e164(g(r, "Phone")) for r in rows) if p])
        already = await imported_job_numbers(c)
        print(f"fields={len(fields)} owen_matches={len(camps)} already_imported={len(already)}")

        for r in rows:
            job = g(r, "Job #")
            if job in already:
                print(f"  [SKIP] {job}")
                continue
            phone = e164(g(r, "Phone"))
            src = g(r, "Source")
            is_ahs = src == "AHS"
            pipe = PIPE_AHS if is_ahs else PIPE_RETAIL
            wstat = g(r, "Status")
            gstatus, ahs_stage, ret_stage = STATUS_MAP.get(wstat, ("open", "New Lead", "New Lead"))
            stage = stages.get(pipe, {}).get(ahs_stage if is_ahs else ret_stage)
            owen_camp = camps.get(phone)
            basis = attribution_basis(src, owen_camp)
            title = " - ".join(x for x in [job, g(r, "Type"), g(r, "Client")] if x)[:100]

            if dry_run:
                print(f"  [DRY] {job:<8} {gstatus:<5} ${money(r):>8,.0f} basis={basis}")
                continue

            first, last = split_name(g(r, "Client"))
            cbody = {"locationId": loc, "firstName": first, "lastName": last,
                     "name": g(r, "Client") or phone, "source": "Workiz Import",
                     "tags": [t.strip() for t in g(r, "Tags").split(",") if t.strip()]}
            if phone:
                cbody["phone"] = phone
            if g(r, "Email"):
                cbody["email"] = g(r, "Email")
            for k, col in (("address1", "Address"), ("city", "City"),
                           ("state", "State"), ("postalCode", "Zip code")):
                if g(r, col):
                    cbody[k] = g(r, col)
            rc = await c.post(f"{base}/contacts/upsert", json=cbody, headers=hdr())
            if rc.status_code not in (200, 201):
                print(f"  [FAIL] {job} contact {rc.status_code}: {rc.text[:140]}")
                continue
            cid = (rc.json().get("contact") or {}).get("id")

            vals = {"workiz_job_number": job, "workiz_job_name": g(r, "Job name"),
                    "workiz_type": g(r, "Type"), "workiz_status": wstat,
                    "workiz_source": src, "workiz_tech": g(r, "Tech"),
                    "workiz_created_by": g(r, "Created by"),
                    "workiz_scheduled": g(r, "Scheduled"), "workiz_end": g(r, "End"),
                    "workiz_job_created": g(r, "Job Created"), "workiz_tags": g(r, "Tags"),
                    "workiz_total": g(r, "Total"), "attribution_basis": basis}
            cf = [{"id": fields[k], "field_value": v}
                  for k, v in vals.items() if fields.get(k) and v]
            if owen_camp and fields.get("owen_campaign"):
                cf.append({"id": fields["owen_campaign"], "field_value": owen_camp})

            obody = {"pipelineId": pipe, "locationId": loc, "name": title,
                     "status": gstatus, "contactId": cid,
                     "monetaryValue": money(r), "customFields": cf}
            if stage:
                obody["pipelineStageId"] = stage
            ro = await c.post(f"{base}/opportunities/", json=obody, headers=hdr())

            if ro.status_code not in (200, 201):
                # GHL permits ONE opportunity per contact. A multi-job client's extra jobs
                # land as a note so the export is never silently truncated.
                if "OPPORTUNITY_NO_DUPLICATE" in (ro.text or ""):
                    note = chr(10).join([
                        f"Workiz job {job} (additional job — GHL allows one opportunity "
                        f"per contact)",
                        f"Type: {g(r, 'Type')}", f"Status: {wstat} -> {gstatus}",
                        f"Scheduled: {g(r, 'Scheduled')}   End: {g(r, 'End')}",
                        f"Total: {g(r, 'Total')}",
                        f"Source: {src}" + (f"   |   OWEN campaign: {owen_camp}"
                                            if owen_camp else ""),
                        f"Tech: {g(r, 'Tech')}", f"Tags: {g(r, 'Tags')}",
                    ])
                    rn = await c.post(f"{base}/contacts/{cid}/notes",
                                      json={"body": note}, headers=hdr())
                    print(f"  [NOTE] {job:<8} duplicate contact -> "
                          f"{'ok' if rn.status_code in (200, 201) else 'FAILED'}")
                    ledger.append({"job": job, "contact": cid, "opportunity": None,
                                   "appointment": None, "note": True})
                else:
                    print(f"  [FAIL] {job} opportunity {ro.status_code}: {ro.text[:140]}")
                continue

            oid = (ro.json().get("opportunity") or {}).get("id")
            appt = None
            st, en = appt_window(g(r, "Scheduled"))
            if st and en and cid:
                ra = await c.post(f"{base}/calendars/events/appointments", headers=hdr(),
                                  json={"calendarId": CALENDAR_ID, "locationId": loc,
                                        "contactId": cid, "startTime": st, "endTime": en,
                                        "title": title, "appointmentStatus": "confirmed",
                                        "ignoreFreeSlotValidation": True, "toNotify": False})
                if ra.status_code in (200, 201):
                    appt = ra.json().get("id")
            ledger.append({"job": job, "contact": cid, "opportunity": oid, "appointment": appt})
            print(f"  [OK]   {job:<8} {gstatus:<5} ${money(r):>8,.0f} basis={basis}")
    return ledger


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path")
    ap.add_argument("--limit", type=int, default=0, help="import at most N rows")
    ap.add_argument("--dry-run", action="store_true", help="plan only, write nothing")
    args = ap.parse_args()

    if not settings.ghl_api_enabled:
        sys.exit("GHL_API_TOKEN / GHL_LOCATION_ID not configured")

    with open(args.csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows)} row(s) from {args.csv_path}")

    ledger = await import_rows(rows, dry_run=args.dry_run)
    print(f"\ncreated/handled: {len(ledger)}")
    print("LEDGER=" + json.dumps(ledger))


if __name__ == "__main__":
    asyncio.run(main())

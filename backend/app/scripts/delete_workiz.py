"""Gate 2 of the Workiz re-import: remove the 2026-07-24 import's footprint from GHL.

    python -m app.scripts.delete_workiz                    # dry run — writes NOTHING
    python -m app.scripts.delete_workiz --execute          # after you approve the list

Dry run is the default and --execute is the only way to delete. There is no undo in GHL.

SCOPE (agreed 2026-07-28) — only what the import created:
  - opportunities whose name still carries the `{Job #} - ` prefix
  - contacts whose `source` is "Workiz Import"
  - the dedicated calendar "Workiz Jobs (imported)", deleted whole (267 events with it)
  - notes, which die with their contact

RUN THIS BEFORE THE RENAME SHIPS. Identification depends entirely on the OLD title format:
once titles become "{Client} - {Type}" there is no job number in the name and nothing here
matches. The 2026-07-24 run printed its ledger to stdout inside a `--rm` container, so no
id list survives — the name prefix is the only handle we have left.

TWO KNOWN IMPRECISIONS, accepted by the owner with this dry-run as the mitigation:

  1. The email->GHL relay stays RUNNING during the delete, so a Dispatch email can recreate a
     contact seconds after we remove it. --execute re-queries afterwards and reports anything
     that reappeared; it cannot prevent it.
  2. `source` is set by whichever write touched the contact LAST, and the relay overwrites it.
     A Workiz contact later touched by a Dispatch email now reads "OWEN Email Ingest" and is
     MISSED here; a relay contact later touched by the import reads "Workiz Import" and would
     be DELETED here, taking its conversation history with it. That is why every contact is
     printed with its tags and why opportunities are cross-referenced against their contact's
     source below — read the CONFLICT section of the dry run properly before approving.
"""

import argparse
import asyncio
import csv
import io
import json
import re
import sys
from collections import Counter

import httpx

from app.core.config import settings
from app.scripts.import_workiz import hdr

# The calendar the 2026-07-24 run created and booked its 267 events on. Hardcoded here rather
# than imported: import_workiz.CALENDAR_ID now points at the REPLACEMENT calendar, and this
# script must delete the old one.
OLD_CALENDAR_ID = "hRPMITl1zpCZQnCByxwV"      # "Workiz Jobs (imported)"

# The old title format: `{Job #} - {Type} - {Client}`, job numbers being 6 alphanumerics.
# Anchored and width-bound so an unrelated card is very unlikely to collide -- but the email
# relay also builds names as "{job_id} {service} - {customer}", so a 6-char Dispatch job id
# COULD match. That is exactly what the contact-source cross-reference below is for.
OLD_TITLE = re.compile(r"^([A-Z0-9]{6})\s-\s")

WORKIZ_SOURCE = "Workiz Import"
RELAY_SOURCE = "OWEN Email Ingest"


async def all_opportunities(c: httpx.AsyncClient) -> list:
    """Every opportunity in the location. Paged; stops on the first empty/short page."""
    out, page = [], 1
    while page <= 100:
        r = await c.get(f"{settings.GHL_API_BASE}/opportunities/search",
                        params={"location_id": settings.GHL_LOCATION_ID,
                                "limit": 100, "page": page}, headers=hdr())
        if r.status_code != 200:
            print(f"  opportunities page {page}: HTTP {r.status_code} {r.text[:160]}")
            break
        batch = r.json().get("opportunities", [])
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return out


async def all_contacts(c: httpx.AsyncClient) -> list:
    """Every contact in the location.

    Filtered client-side on `source` rather than via a server-side query: GHL's contact
    search filters are inconsistent across API versions, and for a decision this destructive
    a full listing we can count and eyeball is worth the extra pages.

    Pagination is deliberately paranoid. The 2026-07-28 execute run stopped after 267
    contacts because the loop broke on a short page — GHL returned fewer than `limit` while
    more still existed. 37 contacts were therefore never enumerated and survived a delete
    that reported 260/260 success. Do NOT reintroduce a short-page break: stop only when a
    page is empty, when the cursor stops advancing, or when meta.total is reached.
    """
    out, seen, after_id, after = [], set(), None, None
    total = None
    while len(out) < 50000:
        params = {"locationId": settings.GHL_LOCATION_ID, "limit": 100}
        if after_id:
            params["startAfterId"], params["startAfter"] = after_id, after
        r = await c.get(f"{settings.GHL_API_BASE}/contacts/", params=params, headers=hdr())
        if r.status_code != 200:
            print(f"  contacts: HTTP {r.status_code} {r.text[:160]}")
            break
        body = r.json()
        batch = body.get("contacts", [])
        meta = body.get("meta") or {}
        if total is None:
            total = meta.get("total")
        fresh = [ct for ct in batch if ct.get("id") not in seen]
        if not fresh:
            break                      # empty page, or the cursor stopped advancing
        seen.update(ct.get("id") for ct in fresh)
        out.extend(fresh)
        if total and len(out) >= int(total):
            break
        after_id = meta.get("startAfterId") or batch[-1].get("id")
        after = meta.get("startAfter") or batch[-1].get("dateAdded")
        if not after_id:
            break
    if total and len(out) < int(total):
        print(f"  WARNING: paged {len(out)} contacts but the API reports {total}. "
              f"The delete below is INCOMPLETE — re-run until this warning disappears.")
    return out


def classify(opps: list, contacts: list, csv_jobs: set) -> tuple:
    """Split matched opportunities into confident deletes and ones needing a human look.

    `csv_jobs` — job numbers from the Workiz export — is the AUTHORITATIVE test. A job number
    present in the export is a Workiz job, full stop, and the contact's `source` says nothing
    about it: the email relay overwrites `source` on any contact it later touches. The
    2026-07-28 dry run found three such cards (4ET7JE, D157RL, FL7NFU), all genuine Workiz
    jobs sitting on contacts the relay had since re-sourced. Judging them by contact source
    would have orphaned them.

    Without a CSV we fall back to the contact-source cross-reference, which is guesswork.
    """
    src_by_contact = {ct.get("id"): (ct.get("source") or "") for ct in contacts}
    confident, conflicted = [], []
    for o in opps:
        m = OLD_TITLE.match(o.get("name") or "")
        if not m:
            continue
        job = m.group(1)
        src = src_by_contact.get(o.get("contactId"), "(contact not found)")
        row = {"id": o.get("id"), "job": job, "name": o.get("name"),
               "contactId": o.get("contactId"), "contact_source": src,
               "value": o.get("monetaryValue"), "status": o.get("status")}
        if csv_jobs:
            (confident if job in csv_jobs else conflicted).append(row)
        else:
            (conflicted if src == RELAY_SOURCE else confident).append(row)
    return confident, conflicted


async def delete(c: httpx.AsyncClient, path: str, ident: str) -> bool:
    r = await c.delete(f"{settings.GHL_API_BASE}{path}", headers=hdr())
    if r.status_code in (200, 201, 204):
        return True
    print(f"    FAILED {ident}: HTTP {r.status_code} {r.text[:120]}")
    return False


async def run(execute: bool, drop_calendar: bool, ledger_path: str,
              csv_jobs: set) -> None:
    async with httpx.AsyncClient(timeout=45) as c:
        print("Reading the location (read-only)...")
        opps = await all_opportunities(c)
        contacts = await all_contacts(c)
        print(f"  {len(opps)} opportunities, {len(contacts)} contacts\n")

        confident, conflicted = classify(opps, contacts, csv_jobs)
        workiz_contacts = [ct for ct in contacts if (ct.get("source") or "") == WORKIZ_SOURCE]

        # ---- what the account actually looks like, so the scope is not taken on trust ----
        print("CONTACTS BY SOURCE")
        for src, n in Counter((ct.get("source") or "(none)") for ct in contacts).most_common():
            mark = "   <-- WILL BE DELETED" if src == WORKIZ_SOURCE else ""
            print(f"  {n:>5}  {src}{mark}")

        print(f"\nOPPORTUNITIES MATCHING THE OLD `{{Job #}} - ` TITLE   "
              f"{len(confident) + len(conflicted)} of {len(opps)}")
        for row in confident[:400]:
            val = f"${float(row['value'] or 0):,.0f}"
            print(f"  {row['job']}  {val:>10}  {row['status']:<5}  {row['name'][:60]}")
        if len(confident) > 400:
            print(f"  ... and {len(confident) - 400} more")

        if conflicted:
            why = ("their job number is NOT in the Workiz export supplied" if csv_jobs
                   else f"their contact is sourced '{RELAY_SOURCE}'")
            print(f"\n  *** EXCLUDED — {len(conflicted)} title-matched opportunities, because "
                  f"{why}.")
            print("      Left in place. If one of these IS a Workiz job it came from an older "
                  "export;\n      check by hand:")
            for row in conflicted:
                print(f"      {row['job']}  src={row['contact_source']:<18} "
                      f"{row['name'][:60]}")

        print(f"\nCONTACTS WITH source == '{WORKIZ_SOURCE}'   {len(workiz_contacts)}")
        relay_tagged = [ct for ct in workiz_contacts
                        if "ahs-job" in [t.lower() for t in (ct.get("tags") or [])]]
        if relay_tagged:
            print(f"  *** {len(relay_tagged)} of them carry the relay's `ahs-job` tag, meaning "
                  f"the email relay\n      touched them too. Deleting these loses email-relay "
                  f"history. Listed in full:")
            for ct in relay_tagged:
                print(f"      {ct.get('id')}  {ct.get('phone') or '(no phone)':<16} "
                      f"{(ct.get('contactName') or ct.get('name') or '')[:40]}")

        # ---- does this location allow more than one opportunity per contact? ----------
        # Answered from data rather than from the settings UI, which does not expose the
        # control at all (checked 2026-07-28).
        #
        # Keyed on (contact, PIPELINE), not contact alone. The first dry run found 6 contacts
        # holding two opportunities and naively read that as "duplicates allowed" — but every
        # pair straddled the AHS and Retail pipelines. GHL's rule is one opportunity per
        # contact PER PIPELINE, so cross-pipeline pairs prove nothing.
        per_pair = Counter((o.get("contactId"), o.get("pipelineId")) for o in opps
                           if o.get("contactId"))
        multi = [k for k, n in per_pair.items() if n > 1]
        cross = len([c for c, n in Counter(o.get("contactId") for o in opps
                                           if o.get("contactId")).items() if n > 1])
        print(f"\nDUPLICATE-OPPORTUNITY SETTING (inferred)")
        print(f"  {cross} contact(s) hold >1 opportunity overall; {len(multi)} hold >1 in the "
              f"SAME pipeline.")
        if multi:
            print("  Same-pipeline duplicates exist -> duplicates are ALLOWED.")
            print("  Run the import with --duplicates allow so every job gets its own card.")
        else:
            print("  No same-pipeline duplicates -> the one-per-contact-per-pipeline rule is "
                  "ON.\n  Keep the default --duplicates sum. It sums PER PIPELINE, so a client "
                  "with both\n  AHS and Retail work still gets one card in each.")

        print(f"\nCALENDAR  {OLD_CALENDAR_ID}  "
              f"{'will be DELETED whole (all its events with it)' if drop_calendar else 'left alone'}")

        planned = {"opportunities": confident, "contacts": workiz_contacts,
                   "calendar": OLD_CALENDAR_ID if drop_calendar else None}
        print(f"\n{'=' * 70}\nPLAN  {len(confident)} opportunities · {len(workiz_contacts)} "
              f"contacts · {1 if drop_calendar else 0} calendar")

        if not execute:
            print("\nDRY RUN — nothing was deleted. Re-run with --execute to apply.")
            with open(ledger_path, "w", encoding="utf-8") as fh:
                json.dump(planned, fh, indent=2)
            print(f"Plan written to {ledger_path}")
            return

        # ------------------------------- destructive -------------------------------
        print("\nEXECUTING. This cannot be undone.\n")
        done = {"opportunities": [], "contacts": [], "calendar": None, "failed": []}

        for row in confident:
            if await delete(c, f"/opportunities/{row['id']}", row["job"]):
                done["opportunities"].append(row)
            else:
                done["failed"].append(("opportunity", row["id"]))
        print(f"  opportunities deleted: {len(done['opportunities'])}/{len(confident)}")

        for ct in workiz_contacts:
            if await delete(c, f"/contacts/{ct['id']}", ct.get("phone") or ct["id"]):
                done["contacts"].append({"id": ct["id"], "phone": ct.get("phone"),
                                         "name": ct.get("contactName") or ct.get("name")})
            else:
                done["failed"].append(("contact", ct["id"]))
        print(f"  contacts deleted:      {len(done['contacts'])}/{len(workiz_contacts)}")

        if drop_calendar:
            if await delete(c, f"/calendars/{OLD_CALENDAR_ID}", OLD_CALENDAR_ID):
                done["calendar"] = OLD_CALENDAR_ID
                print(f"  calendar deleted:      {OLD_CALENDAR_ID}")

        # ---- relay-race check: the worker was left running, so re-read and report ----
        print("\nRe-reading the location to catch anything the email relay recreated "
              "mid-delete...")
        after_opps = await all_opportunities(c)
        after_contacts = await all_contacts(c)
        left, still_conflicted = classify(after_opps, after_contacts, csv_jobs)
        reappeared = [ct for ct in after_contacts
                      if (ct.get("source") or "") == WORKIZ_SOURCE]
        if left or reappeared:
            print(f"  *** {len(left)} old-title opportunities and {len(reappeared)} "
                  f"'{WORKIZ_SOURCE}' contacts are STILL PRESENT.")
            print("      Either a delete failed above, or the relay wrote during the run. "
                  "Re-run this script.")
            for ct in reappeared[:20]:
                print(f"      contact {ct.get('id')}  {ct.get('phone')}")
        else:
            print("  clean — nothing matching remains.")

        done["remaining_after"] = {"opportunities": len(left), "contacts": len(reappeared)}
        with open(ledger_path, "w", encoding="utf-8") as fh:
            json.dump(done, fh, indent=2)
        print(f"\nDeletion ledger written to {ledger_path}")
        if done["failed"]:
            print(f"{len(done['failed'])} deletions FAILED — see the ledger.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true",
                    help="actually delete (default is a dry run that writes nothing)")
    ap.add_argument("--keep-calendar", action="store_true",
                    help="do not delete the dedicated Workiz calendar")
    ap.add_argument("--ledger", default="/data/workiz_delete_ledger.json")
    ap.add_argument("--csv", default="",
                    help="the Workiz export. Job numbers in it are the authoritative test for "
                         "'this opportunity came from the import' — strongly recommended, "
                         "since contact `source` is overwritten by the email relay.")
    args = ap.parse_args()

    csv_jobs = set()
    if args.csv:
        with open(args.csv, encoding="utf-8-sig", newline="") as fh:
            csv_jobs = {(r.get("Job #") or "").strip()
                        for r in csv.DictReader(fh) if (r.get("Job #") or "").strip()}
        print(f"{len(csv_jobs)} job number(s) read from {args.csv}")

    if not settings.ghl_api_enabled:
        sys.exit("GHL_API_TOKEN / GHL_LOCATION_ID not configured")
    asyncio.run(run(args.execute, not args.keep_calendar, args.ledger, csv_jobs))


if __name__ == "__main__":
    main()

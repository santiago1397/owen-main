"""Repair two defects left by the 2026-07-29 import.

    python -m app.scripts.fix_workiz /out/export.csv /out/workiz_ledger.jsonl [--execute]

1. STRANDED VALUE. `--duplicates sum` puts a client's whole won total on the FIRST job for
   their (phone, pipeline). Running the import in two phases broke that: the pilot summed
   over its 10 rows only, so three clients' cards were created carrying $0, and when their
   real total arrived on a later job GHL rejected the second card and the money became a
   note. Fix by writing the correct summed total onto the card that already exists.

   Escala is deliberately NOT fixed this way: the card blocking his job is a Dispatch relay
   card, not ours, and silently folding Workiz revenue into a relay card would corrupt a
   record OWEN does not own.

2. MISSING APPOINTMENT. One job (6IN1HB) lost its appointment to a transient GHL 401.

Dry run by default.
"""

import argparse
import asyncio
import csv
import json

import httpx

from app.core.config import settings
from app.scripts.import_workiz import (
    CALENDAR_ID, PIPE_AHS, PIPE_RETAIL, appt_window, e164, g, hdr, money,
    norm_status, title_for, WON_STATUSES,
)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path")
    ap.add_argument("ledger_path")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    with open(args.csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    by_job = {(r.get("Job #") or "").strip(): r for r in rows}
    led = [json.loads(l) for l in open(args.ledger_path, encoding="utf-8") if l.strip()]
    by_job_led = {e["job"]: e for e in led}
    ours = {e["opportunity"] for e in led if e.get("opportunity")}

    # The authoritative per-(phone, pipeline) won total, computed over the WHOLE export —
    # which is exactly what the split run failed to do.
    won = {}
    for r in rows:
        p = e164(g(r, "Phone"))
        if p and norm_status(g(r, "Status")) in WON_STATUSES:
            key = (p, PIPE_AHS if g(r, "Source") == "AHS" else PIPE_RETAIL)
            won[key] = won.get(key, 0.0) + money(r)

    async with httpx.AsyncClient(timeout=45) as c:
        print("--- 1. stranded value ---")
        for e in sorted((x for x in led if (x.get("stranded") or 0) > 0),
                        key=lambda x: -x["stranded"]):
            row = by_job[e["job"]]
            phone = e164(g(row, "Phone"))
            pipe = PIPE_AHS if g(row, "Source") == "AHS" else PIPE_RETAIL
            target = won.get((phone, pipe), 0.0)

            # Find the card from the LEDGER, not by asking GHL which opportunities a contact
            # has — /contacts/{id}/opportunities returns nothing useful here. A sibling job on
            # the same contact, routed to the same pipeline, and holding an opportunity we
            # created IS the card that blocked this one.
            sibs = [x for x in led
                    if x["contact"] == e["contact"] and x.get("opportunity")
                    and x["job"] != e["job"]
                    and (PIPE_AHS if g(by_job[x["job"]], "Source") == "AHS"
                         else PIPE_RETAIL) == pipe]
            if not sibs:
                print(f"  {e['job']:<8} ${e['stranded']:>8,.0f}  SKIP — no card of ours in "
                      f"that pipeline; the blocker is a non-Workiz card. Left as a note.")
                continue

            rg = await c.get(f"{settings.GHL_API_BASE}/opportunities/{sibs[0]['opportunity']}",
                             headers=hdr())
            if rg.status_code != 200:
                print(f"  {e['job']:<8} GET {rg.status_code}")
                continue
            op = (rg.json() or {}).get("opportunity") or {}
            op["id"] = sibs[0]["opportunity"]
            cur = float(op.get("monetaryValue") or 0)
            print(f"  {e['job']:<8} ${e['stranded']:>8,.0f}  card {op.get('name')!r} "
                  f"${cur:,.0f} -> ${target:,.0f}")
            if args.execute:
                body = {"pipelineId": pipe, "name": op.get("name"),
                        "status": op.get("status"), "monetaryValue": target}
                if op.get("pipelineStageId"):
                    body["pipelineStageId"] = op["pipelineStageId"]
                ru = await c.put(f"{settings.GHL_API_BASE}/opportunities/{op['id']}",
                                 json=body, headers=hdr())
                print(f"           {'ok' if ru.status_code in (200, 201) else ru.text[:120]}")

        print("\n--- 2. missing appointments ---")
        for e in led:
            if e.get("appointment") or not e.get("contact"):
                continue
            row = by_job.get(e["job"])
            st, en = appt_window(g(row, "Scheduled")) if row else (None, None)
            if not (st and en):
                print(f"  {e['job']:<8} no parseable Scheduled — nothing to book")
                continue
            print(f"  {e['job']:<8} book {st} .. {en}")
            if args.execute:
                ra = await c.post(f"{settings.GHL_API_BASE}/calendars/events/appointments",
                                  headers=hdr(),
                                  json={"calendarId": CALENDAR_ID,
                                        "locationId": settings.GHL_LOCATION_ID,
                                        "contactId": e["contact"], "startTime": st,
                                        "endTime": en, "title": title_for(row),
                                        "appointmentStatus": "confirmed",
                                        "ignoreFreeSlotValidation": True, "toNotify": False})
                print(f"           {'ok' if ra.status_code in (200, 201) else ra.text[:140]}")

    if not args.execute:
        print("\nDRY RUN — pass --execute to apply.")


if __name__ == "__main__":
    asyncio.run(main())

"""Gate 3: read the imported records BACK OUT of GHL and compare them to the CSV.

    python -m app.scripts.verify_workiz /out/export.csv /out/workiz_ledger.jsonl

A 200 response is not evidence the data is right — the 2026-07-24 pilot reported 10/10 OK
while writing "AHS â€“ Repair Scheduled" into every record. Nothing here trusts the write
path: every field is re-fetched from GHL and diffed against the source row.

Opportunities are fetched INDIVIDUALLY on purpose. /opportunities/search returns customFields
with no values, so a search-based check would silently pass on empty fields.
"""

import argparse
import asyncio
import csv
import json
import sys

import httpx

from app.core.config import settings
from app.scripts.import_workiz import (
    STATUS_LOOKUP, e164, hdr, money, norm_status, title_for,
)


async def api_get(c, url, params=None, tries=7):
    """GET with 429 backoff.

    The first full-run verification produced 30+ phantom failures — "contact missing the
    workiz-import tag", "phone None" — that were simply throttled responses parsed as empty
    bodies. A verifier that reports false problems is worse than none, so 429 is retried
    rather than interpreted.
    """
    r = None
    for i in range(tries):
        r = await c.get(url, params=params, headers=hdr())
        if r.status_code != 429:
            return r
        await asyncio.sleep(min(float(r.headers.get("Retry-After") or 2 ** i), 30))
    return r


async def field_names(c) -> dict:
    r = await api_get(c, f"{settings.GHL_API_BASE}/locations/{settings.GHL_LOCATION_ID}"
                         f"/customFields", params={"model": "opportunity"})
    return {f.get("id"): f.get("name") for f in (r.json().get("customFields") or [])}


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path")
    ap.add_argument("ledger_path")
    ap.add_argument("--show", type=int, default=3, help="full field dump for the first N")
    args = ap.parse_args()

    with open(args.csv_path, encoding="utf-8-sig", newline="") as fh:
        rows = {(r.get("Job #") or "").strip(): r for r in csv.DictReader(fh)}
    entries = [json.loads(line) for line in open(args.ledger_path, encoding="utf-8")
               if line.strip()]
    print(f"{len(entries)} ledger entries · {len(rows)} CSV rows\n")

    problems, checked = [], 0
    async with httpx.AsyncClient(timeout=45) as c:
        names = await field_names(c)

        for i, e in enumerate(entries):
            row = rows.get(e["job"])
            if not row:
                problems.append(f"{e['job']}: in ledger but NOT in the CSV")
                continue

            # ---- contact ----
            rc = await api_get(c, f"{settings.GHL_API_BASE}/contacts/{e['contact']}")
            if rc.status_code != 200:
                problems.append(f"{e['job']}: contact GET {rc.status_code}")
                continue
            ct = (rc.json() or {}).get("contact") or {}
            tags = [t.lower() for t in (ct.get("tags") or [])]
            if "workiz-import" not in tags:
                problems.append(f"{e['job']}: contact missing the workiz-import tag")
            want_phone = e164((row.get("Phone") or "").strip())
            if want_phone and ct.get("phone") != want_phone:
                problems.append(f"{e['job']}: contact phone {ct.get('phone')} != {want_phone}")

            if not e.get("opportunity"):
                # Note path — verify only that the contact carries a note for this job.
                rn = await api_get(c, f"{settings.GHL_API_BASE}/contacts/{e['contact']}"
                                      f"/notes")
                bodies = " ".join((n.get("body") or "")
                                  for n in (rn.json().get("notes") or []))
                mark = "ok" if e["job"] in bodies else "NOTE NOT FOUND"
                if mark != "ok":
                    problems.append(f"{e['job']}: note path but no note on the contact")
                print(f"  [note] {e['job']:<8} {mark:<14} {ct.get('contactName') or ''}")
                checked += 1
                continue

            # ---- opportunity, fetched individually so customFields carry values ----
            ro = await api_get(c, f"{settings.GHL_API_BASE}/opportunities/{e['opportunity']}")
            if ro.status_code != 200:
                problems.append(f"{e['job']}: opportunity GET {ro.status_code}")
                continue
            op = (ro.json() or {}).get("opportunity") or {}
            got = {names.get(f.get("id"), f.get("id")): f.get("fieldValue", f.get("field_value"))
                   for f in (op.get("customFields") or [])}

            want_title = title_for(row)
            want_status = STATUS_LOOKUP.get(norm_status(row.get("Status")),
                                            ("open",))[0]
            if op.get("name") != want_title:
                problems.append(f"{e['job']}: title {op.get('name')!r} != {want_title!r}")
            if op.get("status") != want_status:
                problems.append(f"{e['job']}: status {op.get('status')} != {want_status}")
            if not op.get("pipelineStageId"):
                problems.append(f"{e['job']}: NO pipeline stage set")
            if got.get("workiz_job_number") != e["job"]:
                problems.append(f"{e['job']}: workiz_job_number = "
                                f"{got.get('workiz_job_number')!r}")
            # The corruption check that actually matters: mojibake in any written field.
            for k, v in got.items():
                if v and ("â€" in str(v) or "Ã¢" in str(v)):
                    problems.append(f"{e['job']}: MOJIBAKE in {k}: {v!r}")

            print(f"  [opp]  {e['job']:<8} ${float(op.get('monetaryValue') or 0):>9,.0f}  "
                  f"{op.get('status'):<5} {(op.get('name') or '')[:44]:<44} "
                  f"basis={got.get('attribution_basis')} camp={got.get('owen_campaign') or '-'}")
            if i < args.show:
                for k in sorted(got):
                    print(f"           {k:<22} {got[k]!r}")
            checked += 1

    print(f"\nchecked {checked}")
    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for p in problems:
            print(f"  {p}")
        sys.exit(1)
    print("\nall verified against the CSV — titles, statuses, stages, fields, tags, encoding")


if __name__ == "__main__":
    asyncio.run(main())

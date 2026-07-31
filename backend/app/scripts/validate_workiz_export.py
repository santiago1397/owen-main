"""Gate 1 of the Workiz re-import: prove the new export is safe BEFORE anything is deleted.

Read-only. Touches neither GHL nor the database. Run this while the old records are still
in GHL and fully recoverable — that is the entire point of it existing.

    python -m app.scripts.validate_workiz_export /data/new_export.csv

It answers the questions that silently corrupted the 2026-07-24 run:

  - is the file actually UTF-8?  A cp1252 mis-read turned "AHS – Repair Scheduled" into
    "AHS â€“ Repair Scheduled" and the import still reported 10/10 OK. We check the raw
    BYTES, never the console rendering.
  - did Workiz rename or drop a column?  A renamed column does not error — it produces a
    blank custom field on all 299 cards.
  - are there statuses we do not map?  An unmapped status silently falls to open/New Lead.
  - do the phones still parse, and does the revenue total look like a full export?

Exit code is 1 if anything BLOCKING is found, so it can gate a scripted run.
"""

import argparse
import csv
import io
import sys
from collections import Counter
from datetime import datetime

# Imported rather than restated: if the import's mapping changes, this validator must move
# with it or it is validating a fiction.
from app.scripts.import_workiz import FMT, STATUS_LOOKUP, e164, money, norm_status

# Columns import_workiz.py actually reads. Anything missing here produces blank data on the
# GHL card without raising, which is why this is a hard failure rather than a warning.
REQUIRED = [
    "Job #", "Client", "Type", "Status", "Source", "Phone", "Total",
    "Scheduled", "Job Created",
]
# Read but tolerable if absent — they only populate custom fields / contact detail.
OPTIONAL = [
    "Job name", "Tech", "Created by", "End", "Tags", "Email",
    "Address", "City", "State", "Zip code",
]

# The 2026-07-24 export, for drift comparison.
BASELINE = {"rows": 299, "clients": 265, "revenue": 267070.0, "done_revenue": 230862.0}


def check_encoding(raw: bytes) -> list:
    """Byte-level encoding verdict. Never trust how a terminal renders the file."""
    notes = []
    if raw.startswith(b"\xef\xbb\xbf"):
        notes.append("  BOM present — utf-8-sig is correct, and the BOM is stripped on read.")
    try:
        raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        notes.append(f"  BLOCKING: not valid UTF-8 — {exc}")
        return notes

    # 0x96 is the cp1252 en-dash. In valid UTF-8 it can only appear as a continuation byte,
    # so its presence alongside a clean UTF-8 decode is worth surfacing explicitly.
    cp1252_dash = raw.count(b"\x96")
    utf8_dash = raw.count(b"\xe2\x80\x93")
    notes.append(f"  decodes as UTF-8 cleanly")
    notes.append(f"  UTF-8 en-dash (E2 80 93): {utf8_dash}   raw 0x96 bytes: {cp1252_dash}")
    if b"\xc3\xa2\xe2\x82\xac" in raw:
        notes.append("  BLOCKING: file already contains mojibake (Ã¢â‚¬) — it was exported "
                     "or saved through a broken encoding. Re-export from Workiz.")
    return notes


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_path")
    args = ap.parse_args()

    raw = open(args.csv_path, "rb").read()
    blocking, warnings = [], []

    print(f"FILE  {args.csv_path}   {len(raw):,} bytes\n")
    print("ENCODING")
    for line in check_encoding(raw):
        print(line)
        if "BLOCKING" in line:
            blocking.append(line.strip())
    if blocking:
        print("\nSTOPPED — fix the encoding before anything else.")
        sys.exit(1)

    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    if not rows:
        sys.exit("BLOCKING: no data rows.")
    cols = list(rows[0].keys())

    print(f"\nSHAPE\n  rows    {len(rows):>6,}   (2026-07-24 export: {BASELINE['rows']})")
    print(f"  columns {len(cols):>6}")

    missing_req = [c for c in REQUIRED if c not in cols]
    missing_opt = [c for c in OPTIONAL if c not in cols]
    unknown = [c for c in cols if c not in REQUIRED + OPTIONAL]
    if missing_req:
        blocking.append(f"missing required columns: {missing_req}")
        print(f"  BLOCKING missing required: {missing_req}")
    if missing_opt:
        warnings.append(f"missing optional columns: {missing_opt}")
        print(f"  WARN    missing optional: {missing_opt}  (those fields import blank)")
    if unknown:
        print(f"  new/unrecognised columns: {unknown}")
    if not missing_req and not missing_opt:
        print("  all known columns present")

    # --- job numbers -----------------------------------------------------------------
    jobs = [(r.get("Job #") or "").strip() for r in rows]
    blank_jobs = sum(1 for j in jobs if not j)
    dupes = [j for j, n in Counter(j for j in jobs if j).items() if n > 1]
    print(f"\nJOB NUMBERS\n  blank {blank_jobs}   duplicates {len(dupes)}")
    if blank_jobs:
        blocking.append(f"{blank_jobs} rows have no Job #")
    if dupes:
        blocking.append(f"duplicate Job #: {dupes[:10]}")
        print(f"  BLOCKING duplicates: {dupes[:10]}")

    # --- statuses --------------------------------------------------------------------
    statuses = Counter((r.get("Status") or "").strip() for r in rows)
    unmapped = {s: n for s, n in statuses.items() if norm_status(s) not in STATUS_LOOKUP}
    print("\nSTATUS")
    for s, n in statuses.most_common():
        known = norm_status(s) in STATUS_LOOKUP
        mark = "" if known else "   <-- UNMAPPED, would import as open/New Lead"
        won = STATUS_LOOKUP.get(norm_status(s), ("open",))[0]
        print(f"  {n:>4}  {s or '(blank)':<32} -> {won}{mark}")
    if unmapped:
        blocking.append(f"unmapped statuses: {list(unmapped)}")

    # --- phones ----------------------------------------------------------------------
    parsed = [e164((r.get("Phone") or "").strip()) for r in rows]
    ok = sum(1 for p in parsed if p)
    print(f"\nPHONES\n  parse to E.164  {ok}/{len(rows)}")
    if ok < len(rows):
        bad = [(r.get("Job #"), r.get("Phone")) for r, p in zip(rows, parsed) if not p][:10]
        print(f"  unparseable (first 10): {bad}")
        warnings.append(f"{len(rows) - ok} phones do not parse — those contacts import "
                        f"without a phone and cannot dedupe or match a campaign")

    # --- dates -----------------------------------------------------------------------
    print("\nDATES")
    for col in ("Job Created", "Scheduled"):
        vals = []
        for r in rows:
            try:
                vals.append(datetime.strptime((r.get(col) or "").strip(), FMT))
            except ValueError:
                pass
        if vals:
            print(f"  {col:<12} {min(vals):%Y-%m-%d} .. {max(vals):%Y-%m-%d}   "
                  f"({len(vals)}/{len(rows)} parsed)")
        else:
            warnings.append(f"no {col} values parsed with the expected format")
            print(f"  {col:<12} BLOCKING none parsed — date format changed?")
            blocking.append(f"no parseable {col}")

    # --- money -----------------------------------------------------------------------
    total = sum(money(r) for r in rows)
    done = sum(money(r) for r in rows if norm_status(r.get("Status")) == "Done")
    clients = len({(r.get("Client") or "").strip() for r in rows})
    print(f"\nREVENUE\n  all rows   ${total:>12,.0f}   (2026-07-24: ${BASELINE['revenue']:,.0f})")
    print(f"  Done only  ${done:>12,.0f}   (2026-07-24: ${BASELINE['done_revenue']:,.0f})"
          f"   <- this is what lands in GHL as won (W4)")
    print(f"  distinct clients {clients}   (2026-07-24: {BASELINE['clients']})")

    if len(rows) < BASELINE["rows"]:
        warnings.append(f"{len(rows)} rows is FEWER than the {BASELINE['rows']} previously "
                        f"imported — is this really a full replacement export, not a delta?")

    # --- multi-job clients (the OPPORTUNITY_NO_DUPLICATE population) ------------------
    by_phone = Counter(p for p in parsed if p)
    multi = {p: n for p, n in by_phone.items() if n > 1}
    print(f"\nMULTI-JOB CLIENTS\n  {len(multi)} phones have >1 job, covering "
          f"{sum(multi.values())} jobs")
    print("  With the GHL duplicate-opportunity toggle OFF, "
          f"{sum(multi.values()) - len(multi)} of those become notes, not cards.")

    print("\n" + "=" * 70)
    for w in warnings:
        print(f"WARN      {w}")
    for b in blocking:
        print(f"BLOCKING  {b}")
    if blocking:
        print("\nDO NOT DELETE ANYTHING. Fix the above and re-run.")
        sys.exit(1)
    print("\nOK — safe to proceed to the delete dry-run." if not warnings else
          "\nOK with warnings — read them before proceeding to the delete dry-run.")


if __name__ == "__main__":
    main()

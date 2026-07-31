# Workiz → GoHighLevel one-off import

> **First executed 2026-07-24. Being redone 2026-07-28** — the first run's records are deleted
> and a fresh export re-imported with customer-name titles. A single historical import of a
> Workiz job export into GHL — not an integration. This is the explicit, bounded exception to
> [`GHL_SYNC_SPEC.md`](GHL_SYNC_SPEC.md) **D21** (OWEN never creates GHL records) and to
> **D14** (Workiz out of scope), which this supersedes for the import only.
>
> Source: `export_export (2).csv` — 299 jobs, 21 columns, exported 2026-07-24.

---

## Second run — EXECUTED 2026-07-29

Source: `export_export (4).csv` — **302 jobs**, 21 columns. The 2026-07-24 records were
removed by `app.scripts.delete_workiz` and re-created by the rewritten
`app.scripts.import_workiz`.

### Result

| | |
|---|---|
| Deleted | 268 opportunities · 260 contacts · the old calendar and its 267 events |
| Imported | **302 of 302 jobs** — 268 opportunities, 34 notes, 302 appointments |
| Verified | 302/302 read back out of GHL and diffed against the CSV — titles, statuses, stages, custom-field values, contact tags, phones, encoding |
| GHL now holds | 270 opportunities (268 ours + 2 Dispatch relay) · AHS 212 · Retail 58 |
| Won in GHL | **$218,737** across 196 cards (open 39 / $32,458 · lost 35 / $7,350) |

**Reconciliation against the CSV's $230,862 of Done work — a $12,125 gap, fully accounted:**

- **$1,125** is genuinely stranded in a note. Guillermo Escala's contact already held a
  *Dispatch relay* opportunity in the AHS pipeline, so GHL rejected his Workiz card. Folding
  Workiz revenue into a relay card would corrupt a record OWEN does not own, so it was left
  as a note deliberately.
- **$11,000** sits on cards whose status is `open`, not `won`. Under `--duplicates sum` a
  client's whole won total lands on the FIRST job for their (phone, pipeline); when that
  first job is itself in progress, the card is open even though the money is earned.
  *Fixable by placing the sum on the client's won job instead — not done, decide first.*

Run 1 under-reported by $32,425 and nobody knew for months. This is $12,125 and it is
printed at the end of every run.

### Things the run discovered

> The GHL-platform findings below are recorded in full, with their consequences for the
> unbuilt sync phases, under **GHL API behaviour** in
> [`GHL_SYNC_SPEC.md`](GHL_SYNC_SPEC.md). Read that one before writing any GHL code; this
> list is the Workiz-shaped summary.

- **A new Workiz status**, `In Progress (Request Approval  )` — with two spaces before the
  paren. Unmapped it would have silently staged 2 jobs as *New Lead*. Both sides now
  normalise whitespace before lookup (`norm_status` / `normStatus`).
- **GHL's rule is one opportunity per contact PER PIPELINE**, not per contact. Six contacts
  held two cards each, every pair straddling AHS and Retail. Summing is keyed accordingly.
- **`source` is not a reliable identifier.** Three genuine Workiz opportunities sat on
  contacts the email relay had re-sourced to `OWEN Email Ingest`. The delete now takes the
  export and treats *job number present in the CSV* as authoritative.
- **Deleting a contact cascades to its opportunities.** One Dispatch relay record (Guillermo
  Escala, $125) was destroyed as collateral, because the import had touched his contact last
  so it read `source: Workiz Import`. Rebuilt with `app.scripts.restore_relay` from the
  parsed email OWEN still held — see that script for the pattern.
- **GHL's search index lags deletes.** The post-delete re-read reported 37 contacts still
  present that were already gone. Treat that check as advisory and re-run to confirm.
- **The API rate-limits at ~900 requests in quick succession** (HTTP 429). `verify_workiz`
  backs off; anything hammering per-record endpoints must too, or it reports phantom
  failures.
- **`/contacts/upsert` 400s when a row has neither phone nor email.** Job 25TNEL — a real
  $100 Done job whose Workiz phone is the placeholder `1111111111` — now falls back to a
  plain create rather than being dropped.

### Scripts

| | |
|---|---|
| `validate_workiz_export.py` | Gate 1. Encoding, columns, statuses, phones, dates, revenue drift. Exits 1 on anything blocking. |
| `delete_workiz.py` | Gate 2. Dry run by default; `--csv` makes identification authoritative. |
| `import_workiz.py` | The import. Ledger-resumable, reports stranded value. |
| `verify_workiz.py` | Gate 3. Reads every record back OUT of GHL and diffs it against the CSV. |
| `fix_workiz.py` | Repairs stranded value and missing appointments after the fact. |
| `restore_relay.py` | Re-relays a parsed Dispatch email whose GHL records were destroyed. |

Artifacts on the server, in `/opt/santiagoproperties/owen-main/workiz_out/`:
`export.csv`, `workiz_ledger.jsonl` (302 entries — **this is how the next cleanup finds these
records**), `workiz_delete_ledger.json`, `workiz_delete_plan.json`.

### Decisions agreed with the owner

| # | Decision |
|---|---|
| **W9** | **Opportunity titles are `{Client} - {Type}`.** The customer's name leads; the job number leaves the title entirely and lives only in `workiz_job_number`. |
| **W10** | **Identity no longer comes from the title.** Three handles replace it: a ledger appended to disk per row, a `workiz-import` **tag** on every contact (tags merge on upsert, `source` does not), and the job number in a custom field. |
| **W11** | **Delete scope is the first import's footprint only** — its opportunities, contacts with `source == "Workiz Import"`, its notes, and the dedicated calendar deleted whole. Not a full account wipe. |
| **W12** | **W4 becomes canonical on both sides.** `frontend/src/lib/workizJobs.ts` was banking `done pending approval`, `Submitted` and `Pending (Collect Balance)` as won; it now agrees with the import that only `Done` is won. The `/campaigns` page reports lower revenue as a result, and that number is the correct one. |
| **W13** | **`owen_campaign` uses the platform's real lead rule** — `_qualified()` from `app/api/attribution.py`, first touch, no date window. See *What was wrong with the first run's attribution* below. |
| **W14** | **Multi-job clients**: if the location's duplicate-opportunity setting can be enabled, every job gets its own card (`--duplicates allow`). Otherwise the client's single card carries the **sum** of their won jobs (`--duplicates sum`, the default), closing the $32,425 hole. |

### Runbook

```
1  python -m app.scripts.validate_workiz_export /data/new.csv     # GATE: approve report
2  python -m app.scripts.delete_workiz                            # dry run
                                                                  # GATE: approve the list,
                                                                  #       read the CONFLICT section
3  python -m app.scripts.delete_workiz --execute
4  create a calendar "Workiz Jobs (imported)"  -> WORKIZ_CALENDAR_ID
5  python -m app.scripts.import_workiz /data/new.csv --limit 10
6  read those 10 records BACK OUT of GHL                          # GATE: approve
7  python -m app.scripts.import_workiz /data/new.csv
```

Step 2 **must run before the rename is deployed** — it finds records by the *old*
`{Job #} - ` title prefix, which the new titles do not have.

Mount a volume for the ledger. The 2026-07-24 run printed its ledger to stdout inside a
`docker compose run --rm` container, so the id list died with the container — which is the
only reason step 2 has to reverse-engineer what to delete from opportunity names.

### What was wrong with the first run's attribution

The original `owen_campaigns()` query was:

```sql
SELECT cl.phone_number, max(cp.name)
FROM callers cl JOIN calls c ON c.caller_id = cl.id AND c.number_id IS NOT NULL
```

Measured against `_qualified()` in `app/api/attribution.py`, which is the platform's actual
definition of a lead, it was missing every filter: no `started_at IS NOT NULL` (so the ~25k
Twilio backfill stubs counted), no duration floor (robocalls counted), no direction filter
(**our own outbound dials counted as the customer calling that campaign**), and no window.
`max(cp.name)` then broke ties alphabetically — in exactly the multi-campaign case the
conflict table below is about.

So the "83 of 264 phones (31%)" figure and every `attribution_basis` value written on
2026-07-24 came from a rule the rest of the platform would reject. Expect the second run to
match **fewer** phones, and some jobs to change campaign. Those numbers are the trustworthy
ones.

---

## First run — 2026-07-24

## Result

| | |
|---|---|
| Opportunities created | **268** of 299 jobs · **0 duplicates** |
| Jobs recorded as notes instead | **31** (GHL one-opportunity-per-contact limit) |
| Contacts | 263 (from 6) |
| Calendar events | 267 on a dedicated calendar |
| Custom fields created | 13 — **12** `workiz_*` plus `attribution_basis`. `owen_campaign` is **not** one of them: spec D15 created it (`LFG2NGPblzA9p03a0p1n`) and the import only reuses it. In a location without the D15 fields it is silently omitted from every card. |
| Status split | won 198 · lost 35 · open 35 |
| Pipeline split | AHS 210 · Retail Repairs 58 |
| Won revenue visible in GHL | **$198,437** |

⚠ **$32,425 of won revenue is NOT in GHL opportunity values** — it belongs to the 31 jobs that
became notes. GHL's pipeline therefore under-reports revenue by ~14% versus the CSV's
$230,862 of Done work. See *Known gaps* below.

## What was in the export

| | |
|---|---|
| Jobs | 299 · 265 distinct clients · 264 distinct phones |
| Revenue | $267,070 — AHS $247,013 (92%) · non-AHS $20,057 |
| Status | Done 225 · Canceled 36 · done-pending-approval 17 · 21 in-progress/pending |
| Phones | 299/299 normalise cleanly to E.164 |
| Dates | Job Created Jan–Jul 2026 · Scheduled Mar 2025 – Jul 2026 |

**Average job value by source** — the commercially significant finding:

| Source | Jobs | Total | Avg |
|---|---:|---:|---:|
| AHS | 241 | $247,013 | **$1,025** |
| Google | 32 | $10,445 | $326 |
| Existing Customer | 17 | $8,412 | $495 |
| CL- ADS | 7 | $1,200 | $171 |

Paid ad channels produce jobs worth **3–6× less** than AHS work. Judging those channels on
lead *count* would badly mislead.

## Decisions

| # | Decision |
|---|---|
| **W1** | **One-off import only.** No recurring Workiz sync; everything after this is enrichment-only per D21. |
| **W2** | Store **both** attributions: `workiz_source` as logged, `owen_campaign` where OWEN's call data proves it. `attribution_basis` records which: `call-verified` \| `conflict` \| `enriched` \| `workiz-only`. |
| **W3** | Route by **workflow, not acquisition**: AHS jobs → *Dream Team Roofing AHS*; the rest → *Retail Repairs*. Ad-sourced AHS jobs still go to AHS; acquisition lives in `owen_campaign`. |
| **W4** | **Done = won** · **done-pending-approval = open** (revenue-at-risk) · **Canceled = lost**. |
| **W5** | Calendar appointments for all jobs **and** full custom fields. |
| **W6** | Import **all 58 tags** as real GHL tags, including 25 one-off free-text notes. |
| **W7** | Contacts get everything: name, phone, email, service address, merged tags, `source = "Workiz Import"`. |
| **W8** | Execute as **pilot 10 → owner inspects → remainder**. |

### W4 status → stage mapping

| Workiz status | n | GHL status | AHS stage | Retail stage |
|---|--:|---|---|---|
| Done | 225 | **won** | Submit The Invoice | Closed |
| Canceled | 36 | **lost** | New Lead | Closed |
| done pending approval | 17 | open | Request the Approval (AHS) | Proposal Sent |
| Pending (Estimate Follow Up) | 5 | open | New Lead | Contacted |
| In Progress (Inspections) | 5 | open | Inspection | Contacted |
| In Progress (Repair Schedule) | 4 | open | Approved- Repair Schedule | Proposal Sent |
| Pending (New Roof Estimate) | 4 | open | New Lead | Proposal Sent |
| Submitted | 1 | open | Submit The Invoice | Proposal Sent |
| In Progress (Callback) | 1 | open | Call Back | Contacted |
| Pending (Collect Balance) | 1 | open | Submit The Invoice | Closed |

## How it was done

Everything ran from a **throwaway container on prod** (`docker compose run --rm --no-deps app`)
so the running services were never touched, and so the script had both the GHL token and the
OWEN database in one process.

```
1. READ the CSV as utf-8-sig
2. CREATE 13 workiz_* opportunity custom fields (idempotent by name)
3. CREATE a dedicated calendar "Workiz Jobs (imported)"   id hRPMITl1zpCZQnCByxwV
4. LOOK UP each phone in OWEN's callers/calls to resolve owen_campaign
5. per job:
     a. POST /contacts/upsert          (dedupes on phone)
     b. POST /opportunities/           (pipeline + stage + status + value + custom fields)
        └─ on OPPORTUNITY_NO_DUPLICATE → POST /contacts/{id}/notes instead
     c. POST /calendars/events/appointments   (2-hour slot from Scheduled)
6. EMIT a ledger of every id created
```

Executed as: **pilot 10** → inspected → **batch 1 (100)** → **batch 2 (100)** →
**batch 3 (89)**. Batching kept each run inside a sane timeout and gave checkpoints.

### Field mapping

| GHL | Source |
|---|---|
| contact name / phone / email | `Client` / `Phone` (E.164) / `Email` |
| contact address | `Address`, `City`, `State`, `Zip code` |
| contact tags | `Tags`, split on comma (merged across that client's jobs) |
| opportunity name | `{Job #} - {Type} - {Client}` |
| monetaryValue | `Total` |
| status + stage | `Status` via the W4 table |
| pipeline | `Source == "AHS"` ? AHS : Retail Repairs |
| appointment start/end | `Scheduled` → `Scheduled + 2h` |
| `workiz_*` fields | the raw column values, verbatim |
| `owen_campaign` | OWEN's campaign for that caller, where a call proves it |

## Gotchas — every one of these was found by the pilot

### The file is UTF-8. An earlier version of this doc said CP1252; that was wrong.
The file contains `E2 80 93` (UTF-8 en-dash) and **no** `0x96` (CP1252 en-dash). The
mis-diagnosis came from a Windows console that cannot render `–` and printed a replacement
character, which looked like corruption. "Fixing" it by reading CP1252 is what actually
corrupted the data — the first pilot wrote `AHS â€“ Repair Scheduled` into 10 records.

**The import reported 10/10 OK while writing corrupted text.** It was only caught by reading
records back out of GHL. *Never trust console rendering to diagnose an encoding — check the
raw bytes.*

### Workiz `End` is a job-CLOSURE timestamp, not an appointment end
```
Scheduled → End duration:   ≤4h: 50   4–24h: 9   1–7d: 29   >7 DAYS: 211 (71%)
worst: 309.9 days (Mar 2025 → Jan 2026)
```
Booking it literally would have created 211 calendar blocks spanning weeks to ten months.
Appointments use a fixed **2-hour** slot from `Scheduled`; the true `End` is preserved in
`workiz_end`.

### GHL allows only ONE opportunity per contact
`POST /opportunities/` returns `400 OPPORTUNITY_NO_DUPLICATE` for a contact that already has
one. This hit the **28 multi-job clients**. Those jobs are recorded as **notes on the contact**
carrying type, status, schedule, total, source and tags — so nothing from the export is lost,
but see *Known gaps*.

### `/opportunities/search` returns `customFields` WITHOUT values
This broke the idempotency check, which looked for `workiz_job_number` in search results and
always found nothing. Consequence: re-running a batch re-attempted jobs that already had
opportunities, and each hit the duplicate error and wrote a **spurious note** (~16 contacts).

**Use the opportunity NAME prefix (`{Job #} - `) to detect what is already imported**, or fetch
opportunities individually — the single-opportunity GET *does* return field values.

### GHL opportunities have no `tags` field
Tags are contact-level. Per-job tags merge onto one contact for multi-job clients; the verbatim
per-job string is kept in `workiz_tags` on each opportunity.

### `assignedUserId` does not stick on appointments
Set on create, returns `None`, even after adding the user as a calendar team member. Left as-is
— cosmetic for a historical import. Real technicians are in `workiz_tech` and are not GHL users
anyway (Antonio Brown 122 jobs, NIco 32, Shay 5 — only "Owen Buzaglo" matches a GHL user).

### Workiz `Source` is unreliable; OWEN can prove it
83 of 264 phones (31%) exist in OWEN's call records. Where OWEN knows which **tracking number
was physically dialled**, it often contradicts Workiz's hand-entered `Source`:

```
Workiz says        OWEN's dialled number says     jobs
Google          →  Craiglist                         3
Google          →  DTR                              10
CL- ADS         →  GBP                               1
AHS             →  DTR / GBP / Craiglist            27
```
`+19542135057` and `+17868049622` are logged **Google** but dialled the **Craigslist** number.
Also: **27 jobs marked "AHS" came from customers acquired via paid tracking numbers** — ad
spend generating AHS work that gets no credit today.

### Money data is thin
The only money column is `Total`. No cost, margin, payments, balance, line items or tax — so
revenue is reportable, **profit is not**. "Paid" exists only as a tag on 26 jobs ($41,728).
**All 13 New Roof Replacements show $0** — the highest-ticket work type recording no revenue,
almost certainly unpriced estimates.

## Known gaps

1. **$32,425 of won revenue is not in opportunity values** — it sits in the 31 duplicate-contact
   notes. To fix, either add those amounts to the existing opportunity's `monetaryValue`
   (conflates several jobs into one card) or accept notes as the record. **Not decided.**
2. **~16 spurious notes** on contacts whose job *does* have an opportunity, from re-running a
   batch before the idempotency flaw was understood. Harmless but untidy.
3. **A junk tag `ahs â€“ repair scheduled`** may remain in the account tag list from the first
   corrupted pilot run. Contacts no longer reference it. Deleting needs a `tags` scope or a
   manual removal in the UI.
4. **The export is a snapshot** — newest job created 2026-07-23 09:34. Anything booked after
   that is absent, and per W1 there is no recurring sync.

## If this is ever re-run

- Read as **utf-8-sig**.
- Detect already-imported jobs by **opportunity name prefix**, not by search customFields.
- Expect `OPPORTUNITY_NO_DUPLICATE` and handle it deliberately.
- Book appointments from `Scheduled` only; never trust `End`.
- Pilot a handful first and **read the records back from GHL** — a 200 response does not mean
  the data is right.

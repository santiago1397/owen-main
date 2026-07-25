# Workiz → GoHighLevel one-off import

> **Executed 2026-07-24.** A single historical import of a Workiz job export into GHL — not an
> integration. This is the explicit, bounded exception to
> [`GHL_SYNC_SPEC.md`](GHL_SYNC_SPEC.md) **D21** (OWEN never creates GHL records).
>
> Source: `export_export (2).csv` — 299 jobs, 21 columns, exported 2026-07-24.

## Result

| | |
|---|---|
| Opportunities created | **268** of 299 jobs · **0 duplicates** |
| Jobs recorded as notes instead | **31** (GHL one-opportunity-per-contact limit) |
| Contacts | 263 (from 6) |
| Calendar events | 267 on a dedicated calendar |
| Custom fields created | 13 `workiz_*` + `attribution_basis` |
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

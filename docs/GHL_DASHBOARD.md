# GHL custom dashboard — build sheet (test build)

> Design settled by interview 2026-07-29. Companion to [`GHL_SYNC_SPEC.md`](GHL_SYNC_SPEC.md)
> and [`WORKIZ_IMPORT.md`](WORKIZ_IMPORT.md). This is a **deliberately narrow** dashboard: it
> shows only what GHL can state truthfully about the imported data. The exclusions below are
> not oversights — each one is a measured defect, recorded so the next person doesn't "fix"
> the dashboard by adding a chart that lies.

## Scope decision

GHL holds **270 opportunities** representing **302 Workiz jobs**. It cannot hold them at job
granularity: GHL enforces **one opportunity per contact per pipeline**, which is why the import
ran `--duplicates sum`. Consequences, measured against `workiz_out/export.csv`:

- 60 jobs collapse into 28 cards (24 pairs, 4 triples).
- **$38,150 of $230,862 won work (16.5%)** sits on those collapsed cards.
- **11 of the 28** blend jobs that disagree on `Source` or `Type`.
- Every opportunity's `createdAt` is **2026-07-29**, the import date — `import_workiz.py:408`
  sets no `createdAt`, and GHL treats it as server-owned.
- `opportunity.source` is **empty on all 270** — verified live via `/opportunities/search`.

### What this dashboard MAY show

| Widget | Why it's safe |
|---|---|
| Won / open / lost value and count | `monetaryValue` + `status` are native and correct |
| Value and count by stage | native, per-card, no blending issue |
| AHS vs Retail split | pipeline is native |

### What this dashboard must NEVER show

| Chart | Why it lies |
|---|---|
| Anything over time | all 270 cards share one creation date |
| Job counts | says 270, truth is 302 |
| Average deal value | inflated on 28 collapsed cards |
| Revenue by lead source / technician / job type | wrong on ~16% of revenue; lives only in opportunity custom fields, which dashboard widgets cannot group by |

**The `source` backfill considered on 2026-07-29 was rejected.** Writing `workiz_source` onto
the native `source` field would make a revenue-by-source pie chart possible and wrong — on the
11 mixed cards, two or three jobs' money would carry one job's source label. That is exactly the
last-touch corruption `GHL_SYNC_SPEC.md` **D4** exists to prevent. Revenue-by-anything belongs
in the CSV or in OWEN, where one row per job is free.

## Widgets to create

Dashboard name: `Pipeline Health (test)`.

1. **KPI — Won value.** Opportunities, status = won, all time. Expect **$218,737 / 196 cards**.
2. **KPI — Open pipeline value.** status = open. Expect **$32,458 / 39**.
3. **KPI — Lost value.** status = lost. Expect **$7,350 / 35**.
4. **Pie — value by pipeline.** Expect AHS **$238,488 / 212**, Retail **$20,057 / 58**.
5. **Bar — count and value by stage, AHS.** Expect the distribution in the table below.
6. **Bar — count and value by stage, Retail Repairs.**
7. **Funnel — AHS stage order.** Reads as a snapshot, not a conversion rate; see caveat below.

Set every widget's date range to **All time**. Any narrower range either returns everything or
nothing, for the `createdAt` reason above.

## Verification numbers

Pulled live from `/opportunities/search` on 2026-07-29. If a widget disagrees with these, the
widget is misconfigured — these are the API's own numbers.

| Pipeline | Status | Cards | Value |
|---|---|---|---|
| Dream Team Roofing AHS | won | 180 | $204,976 |
| Dream Team Roofing AHS | open | 24 | $27,662 |
| Dream Team Roofing AHS | lost | 8 | $5,850 |
| Retail Repairs | won | 16 | $13,761 |
| Retail Repairs | open | 15 | $4,796 |
| Retail Repairs | lost | 27 | $1,500 |
| **All** | **won** | **196** | **$218,737** |
| **All** | **open** | **39** | **$32,458** |
| **All** | **lost** | **35** | **$7,350** |

### By stage

| Pipeline | Stage | Cards | Value |
|---|---|---|---|
| AHS | Submit The Invoice | 180 | $204,976 |
| AHS | Request the Approval (AHS) | 16 | $23,212 |
| AHS | New Lead | 13 | $8,200 |
| AHS | Call Back | 2 | $2,100 |
| AHS | Inspection | 1 | $0 |
| Retail | Closed | 43 | $15,261 |
| Retail | Proposal Sent | 9 | $4,796 |
| Retail | Contacted | 6 | $0 |

**Funnel caveat:** the import placed each card in the stage matching its final Workiz status, so
the "funnel" is a snapshot of where work ended, not a record of cards descending through stages.
183 of 270 cards sit in a terminal stage. Real funnel conversion only becomes meaningful for
cards created *after* the dashboard exists.

Unused pipelines `Marketing Pipeline` and `Local Garage Door` hold 0 opportunities; exclude them
or the pie gains two empty slices.

## Known understatement — fix before anyone trusts this

The won total reads **$218,737**; the CSV's `Done` work is **$230,862**. The **$12,125** gap:

| Amount | Cause | Status |
|---|---|---|
| $4,550 | 3 jobs (`OYQFN1` $2,450, `N3TX26` $1,100, `S8TJN4` $1,000) whose money became a note when GHL rejected a second card | `fix_workiz.py` repairs this — **written, never run, not deployed to the server** |
| $1,125 | `OG7KOZ` (Escala) — blocked by a Dispatch relay card OWEN does not own | deliberate, permanent exclusion |
| ~$6,450 | `--duplicates sum` put a client's whole won total on a job that is still `open` | **undecided** — moving the sum onto the client's *won* job would fix it |

`WORKIZ_IMPORT.md` records that run 1 under-reported by $32,425 and nobody noticed for months.
Until the rows above are resolved, this dashboard is a test surface, not a reporting source.

Note also that the ledger's four stranded jobs total **$5,675**, while `WORKIZ_IMPORT.md`
narrates only $1,125 as stranded. Those two accounts have not been reconciled.

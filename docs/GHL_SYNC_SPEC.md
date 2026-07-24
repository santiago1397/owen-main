# OWEN ↔ GoHighLevel two-way sync — agreed design

> Outcome of a design interview (2026-07-24). Every decision below was explicitly chosen,
> not assumed. Where a decision has a non-obvious rationale it is recorded, because the
> obvious alternative is usually the one that silently corrupts the numbers.
>
> Companion docs: [`CODE_MAP.md`](CODE_MAP.md) (how OWEN works today — note it predates the
> BulkVS/Asterisk platform half), [`../ARCHITECTURE.md`](../ARCHITECTURE.md).

## The question this answers

> "How many leads per number and per campaign, what's the state of the pipeline, and how
> many jobs did we close from each lead source?"

Today OWEN can answer the first half and **none** of the second. It knows every call, its
tracking number, its campaign, whether the caller was new, and what the call was about. It
has no idea what happened next, because the outcome — the job closing — is born in GHL and
nothing ever comes back.

## What exists today (verified in code)

| Path | Mechanism | Live in prod? |
|---|---|---|
| Parsed job emails → GHL | Direct v2 API (`providers/ghl_api.py`, PIT auth): Contact upsert + Opportunity + note | **YES** — 1 email relayed to date |
| Completed calls → GHL | Inbound-Webhook trigger code exists | **NO** — `GHL_CALL_WEBHOOK_URL` is empty; never fired |
| Inbound SMS → GHL | Inbound-Webhook trigger code exists | **NO** — `GHL_INBOUND_WEBHOOK_URL` is empty; never fired |
| GHL → OWEN | *nothing* | — |

**Verified in prod 2026-07-24:** `calls.relayed_to_ghl = 0`, and **no `call_relay_ghl` job has
ever been enqueued**. The premium-webhook paths were built but never switched on — so no
premium spend has ever been incurred, and **D2 requires no migration**: Phase 2 is simply
"build the direct-API push", not "replace the paid one".

Whole-account GHL inventory at that date: **6 contacts, 5 opportunities** — 1 from the Dispatch
email relay (AHS pipeline), 4 GHL "(Example) Deal with…" demo records (Marketing pipeline).
**Retail Repairs — the pipeline chosen in D19 — is empty.** Nothing OWEN-generated exists in
GHL yet; the D3/D6 push is unbuilt.

## Decisions

### D1 — GHL is the workspace; OWEN is the analytics brain
Two-way sync. OWEN pushes leads + attribution into GHL so the team works one pipeline; GHL's
opportunity outcomes are pulled back into OWEN; **all reporting is built in OWEN**.

*Why:* OWEN's Postgres can answer "leads per number per campaign, spam excluded, by day" in
one query. GHL's reporting is contact/opportunity-shaped — once a call becomes a contact, the
per-number, per-campaign, spam-filtered granularity is flattened into tags you pivot by hand.

### D2 — No premium GHL actions, anywhere
`GHL_CALL_WEBHOOK_URL` and `GHL_INBOUND_WEBHOOK_URL` are **retired**. All pushes move to
`ghl_api.py` + the Private Integration Token, exactly as the email relay already does. Inbound
texts reach the GHL conversation view via the API's inbound-message logging endpoint.

*Why:* the premium trigger was never required — the email relay proves the free path works.

### D3 — Tiered lead rule
```
CONTACT       every non-spam caller
OPPORTUNITY   when ANY of:
                • call_analysis.tags contains "job"
                • status is missed / no-answer / busy
                • answered AND duration > 30s
SKIP          is_spam, duration ≤ 1s (misdial), category = wrong-number
```

*Why the missed-call clause:* the existing `job` tag requires `gave_address AND
requested_service`, judged from a transcript. **Missed calls have no recording, therefore no
transcript, therefore no analysis, therefore never a `job` tag.** Gating opportunities on the
tag alone would make every busy-signal and after-hours call invisible — systematically
undercounting the campaigns that ring the most, i.e. making your best ads look worst.

`handle_call_relay_ghl` already skips its analysis-wait when a call has no recording, so
missed calls relay promptly without change.

### D4 — OWEN owns attribution; GHL IDs are the join
New columns on `calls`: `ghl_contact_id`, `ghl_opportunity_id`. Attribution
(`campaign_id`, `number_id`, `is_new_for_campaign`) stays authoritative **in OWEN** and is
pushed to GHL as display-only custom fields on the Opportunity. Reports never read attribution
back out of GHL.

*Why not on the Contact:* contacts are deduped and overwritten. A repeat caller who dials the
Craigslist number in March and the Facebook number in June would overwrite March's value —
silently rewriting every historical report, with last-touch always winning. Opportunities are
per-lead-event and never overwritten.

*Why not a phone-number join:* breaks on formatting variance (`+13055551234` vs
`(305) 555-1234`), contact merges, number changes, and shared household phones.

### D5 — Separate "Inbound Leads" pipeline
AHS/Dispatch jobs stay in `Dream Team Roofing AHS` (`TRbZj4CJ88qZJqr1TRGA`). Ad-call leads get
their own pipeline: `New → Contacted → Quoted → Won / Lost`.

*Why:* AHS jobs are **contractually assigned work** with no ad source, no tracking number and
no campaign. Ad calls are **demand you paid to generate**. Mixing them puts sourceless jobs in
the denominator of "close rate by lead source" — the number looks great and means nothing.

### D6 — Opportunity reuse: open AND recent
```
qualifying call from a known contact:
    open opportunity, updated < 90 days ago?
        yes → log a touch on THAT opportunity
        no  → create a new opportunity
```

*Why open/closed as the discriminator:* an open opp means the job is live (Wednesday's
callback about Monday's roof is a touch, not a second lead); a won/lost opp means the work is
finished, so a call eight months later is genuinely new business.

*Why the 90-day staleness guard:* if the team doesn't actually close cards in GHL, everything
stays open forever and a real new job a year later gets merged into a zombie card nobody has
touched — quietly eating a sale.

### D7 — Back-sync by polling
The existing APScheduler polls GHL every ~10 min for opportunities updated since a stored
cursor (`app_settings` key), matching on `ghl_opportunity_id`.

*Why not webhooks:* GHL's workflow **Custom Webhook action is premium**, billed per execution
— on the highest-frequency event in the system, reintroducing exactly the cost D2 removes. A
marketplace app gets free native webhooks but requires an OAuth install flow and token refresh
instead of a simple PIT. Polling is also **self-healing**: a missed cycle catches up on the
next one, whereas a dropped webhook is lost with no replay. Reporting is retrospective; a
10-minute lag is irrelevant.

### D8 — `status` is authoritative for closure
```
CLOSED   status ∈ (won, lost, abandoned)     ← authoritative
FUNNEL   pipelineStageId                      ← recorded, for where-deals-die analysis
REVENUE  monetaryValue where > 0
         won cards with no value → reported as a data-quality count, NEVER as $0
```

*Why:* `status` and `pipelineStageId` are independent and can disagree — a rep can drag a card
into a "Won" column while `status` stays `open`, or vice versa. Whichever OWEN reads, the other
drifts. `status` wins because it's what GHL's "mark as won" action sets.

*Why the \$0 flag:* `monetaryValue` is optional and usually left at 0. Summing it blindly makes
genuinely-won jobs read as \$0 revenue, and you'd conclude the ads don't work. A won card with
no value is a **data-quality problem, not a zero-dollar sale**.

### D9 — Contacts-only backfill
Historical callers are pushed as GHL Contacts (so the team has context on a repeat call).
**Zero historical opportunities.** Close-rate reporting starts at go-live and is labelled with
that date.

*Why:* backfilled opportunities have no outcomes — nobody worked those cards — so every one
would land `open` and never close, making all historical periods read **0% close rate** while
looking like real data. That is worse than no history: it's history that lies. And it's moot,
because OWEN already holds every historical call with full campaign attribution — historical
*volume* is a SQL query away. The only thing missing from history is outcomes, which is
exactly what backfill cannot manufacture.

### D10 — Queue: concurrency + priority (prerequisite)
```python
# worker.py
for _ in range(WORKER_CONCURRENCY):
    asyncio.create_task(drain_loop())

# queue.claim_one
ORDER BY priority DESC, run_after      # ghl_* = 100, transcribe/recording_fetch = 0
```

*Why this is a blocker, not a nice-to-have:* `drain_loop` claims **one** job, awaits its
handler to completion, and loops — a single serial consumer. `claim_one` orders by `run_after`
alone, strict FIFO, no priority. A `transcribe` job is an OpenAI call on a multi-minute
recording (tens of seconds). With a recorded backlog of ~11,800 pending jobs, a GHL sync job
enqueued today sorts *behind all of them* and would land in GHL days late — and GHL would get
the blame.

`claim_one` already uses `FOR UPDATE SKIP LOCKED` and its docstring says *"multiple drainers
never grab the same job."* **The queue was built for concurrency; nothing ever ran more than
one drainer.** This change is the design finally being used as intended.

### D11 — OpenPhone = follow-up touches, never leads
OpenPhone outbound calls ingest as a distinct provider, `direction=outbound`, **no campaign
attribution**, and are **excluded from every lead count**. They join to existing `callers` by
phone number, becoming recorded touches on known leads.

*Why not a normal fourth provider:* a job closed after nine follow-up calls would report as
nine leads, and outbound calls have no `to_number` for a campaign to key on.

*What it unlocks:* time-to-first-callback, touches-before-close, and **leads with zero
callbacks** — usually the most expensive number a contractor doesn't have.

#### D11a — VERIFIED against the live account (read-only probe, 2026-07-24)

Run from a throwaway `docker compose run --rm` container on prod. GET requests only; nothing
was sent, dialled or written. Findings supersede the guesses this spec was written on:

| Fact | Value |
|---|---|
| Auth header | **raw key** in `Authorization` — *not* `Bearer <key>` |
| Account inventory | **1 number**: `PNRxH5G3uI`, "Business development", ends 7244 |
| Rate limit | **10 req/sec** (`ratelimit-policy: q=10; w=1`) |
| `GET /contacts` | works — `defaultFields{firstName,lastName,phoneNumbers[],emails[]}`, plus `customFields`, `externalId`, `source` |
| `GET /calls` | works, but **`participants` is a REQUIRED array param** |
| Call fields | `direction, status, duration, createdAt, answeredAt, completedAt, participants[], userId, phoneNumberId, aiHandled, callRoute, forwardedFrom/To` |
| `GET /call-recordings/{id}` | **200** — `{url (share.quo.com), type audio/mpeg, duration, status}` |
| `GET /call-transcripts/{id}` | **200** — `{dialogue[{identifier, start, end, content}], duration, status}` |
| `GET /call-summaries/{id}` | **200** — schema `{summary, nextSteps, jobs, status}` |

**Constraint that reshapes the design: there is no time-based sweep.** `GET /calls` rejects
`since`/`createdAfter` without `participants` (HTTP 400, confirmed both spellings). You cannot
ask "all calls on this number since X". Ingestion must therefore be **contact-driven**: for
each OWEN caller we care about, ask OpenPhone "were there calls with this number?".

That happens to fit D11 exactly — we only ever wanted touches on *known leads*, never a full
call dump. But it costs **one request per contact per poll**, so the poll set must be scoped
(e.g. callers with an open opportunity, or activity in the last N days) rather than sweeping
every caller OWEN has ever seen. At 10 req/s this is comfortable for hundreds of contacts,
not for tens of thousands.

**Bonus capability — bigger than expected.** OpenPhone exposes recordings, and
**speaker-labeled transcripts already diarized by phone number** (`dialogue[].identifier` is
the participant's number). That maps directly onto OWEN's existing
`transcriptions.segments` shape (`{speaker,start,end,text}`) — **no new schema, and no
transcription cost**, because OpenPhone has already done the STT. Follow-up call *content*,
not just metadata, can ride into OWEN's existing analysis pipeline.

**Unresolved:** `summary` / `nextSteps` / `jobs` came back empty on the sampled call — it was
a 35s outgoing call that hit the customer's voicemail greeting, so there was no conversation
to summarize. The schema is present; whether it populates on real conversations (and whether
`jobs` is useful for job-status tracking) needs a richer sample before relying on it. **Do not
design against `jobs` until confirmed.**

### D17 — GHL decides; call signals are advisory only

The operating model, stated by the owner: **closure and payment are decided by a human in
GHL.** Information derived from calls tells the team *where a job is between those decisions*
— it never makes the decision.

Therefore, and this is a hard separation:

| Layer | Source | Authority | Used for |
|---|---|---|---|
| **Outcome** | GHL `status` + `monetaryValue` (D8) | **authoritative** | close rates, revenue, won/lost — *all reporting numbers* |
| **Progress signal** | LLM over Quo call transcripts | **advisory** | "where we're at", attention list — *never counted as an outcome* |

This mirrors the existing `call_analysis` pattern exactly: the model proposes
(`category`, `is_spam`), the human disposes (`category_override`, `is_spam_override`), and
reads resolve with `coalesce(override, model_value)`. Same principle, one level up.

**Rules:**
- A call-derived signal **never** moves a GHL card, changes an opportunity's status, or enters
  a close-rate or revenue calculation.
- The UI must render advisory status as visibly distinct from GHL truth — never merged into
  one field, so nobody mistakes a model's guess for a booked job.
- **Divergence is the payoff.** Call signals indicating progress while the GHL card has been
  static for N days = a **stale card**, surfaced in the D12 attention list. This is the single
  most valuable thing the advisory layer produces: it tells you where GHL is out of date,
  which is precisely where revenue leaks.

**Where the signal is shown:** on the GHL Opportunity card, in the dedicated `owen_call_signal`
+ `owen_signal_at` custom fields (D15) — *not* merged into any GHL-native field, and *not*
only in OWEN.

*Why on the card:* the decision is made by a human in GHL. A signal that lives only in OWEN's
dashboard is in the wrong building at the moment that decision happens, and would go unread.
*Why a dedicated, explicitly-named field:* it sits beside GHL's real status without ever being
mistakable for it. The field name carries the `owen_` prefix and the value carries its own
"(unverified)" marker, so the distinction survives someone glancing at the card in a hurry.

### D21 — OWEN NEVER creates records in GHL. Enrichment only. ⚠ SUPERSEDES D3/D6/D9

Owner decision, 2026-07-24: **do not create contacts or opportunities from calls.** GHL
records are created by humans (or by the existing Dispatch-email relay). OWEN's job is to
**find the matching record and add information to it**.

```
BEFORE (D3/D6 as originally agreed)     AFTER (D21)
  qualifying call → create contact        qualifying call → find EXISTING contact
                  → create opportunity                    → if none, do nothing in GHL
                                                          → if found, ENRICH it
```

**What OWEN writes to GHL:** only the `owen_*` custom fields (D15) and notes, and only on
records that already exist. Never a `POST /contacts/upsert`, never a `POST /opportunities/`.

**Matching:** OWEN caller `phone_number` → GHL contact phone. When the matched contact has an
opportunity, enrich the opportunity too (attribution + `owen_call_signal`); otherwise enrich
the contact alone.

**Impact on earlier decisions:**
- **D3** — the tiered lead rule still runs, but it now decides *what OWEN reports as a lead*,
  not what gets created in GHL. Qualification lives entirely in OWEN.
- **D6** — opportunity reuse logic is no longer about whether to *create*; it selects *which*
  existing opportunity to enrich (still: open, and most recently updated).
- **D9** — the historical **contact backfill is CANCELLED**. No bulk import of the ~3,383
  callers. GHL's contact list stays exactly as the team built it.

**The honest cost:** a qualified lead that nobody ever entered into GHL has **no outcome**, and
never will. Close-rate reporting therefore covers only leads that reached GHL.

**The unexpected benefit — a metric that did not exist before.** The gap between "OWEN
qualified this as a lead" and "a GHL record exists for it" is now *measurable*, and it is
exactly the leak the D12 attention list is for:

```
Craigslist 2, last 30d
  qualified leads (OWEN)        18
  reached GHL (matched)         11   ← 61%
  won (GHL)                      4
  ⚠ 7 qualified leads never entered into GHL
```

Because of this, **D12 must report two close rates, never one**:
- *worked* close rate = won ÷ leads that reached GHL — how good the team is at closing
- *true* close rate = won ÷ all qualified leads — how good the business is at converting demand

Reporting only the first would flatter the numbers by hiding every lead that was dropped
before anyone touched it.

### D12 — Reporting: source funnel + actionable leaks list
```
SOURCE PERFORMANCE                         last 30d
Campaign        Calls  Leads  Won   Revenue
Craigslist 2      43     18     6   $24,300
Facebook          27      9     1    $3,100
Organic           11      7     4   $18,900

⚠ NEEDS ATTENTION
  4 leads never called back (>24h)      ← from D11 OpenPhone touches
  3 stalled in Quoted (>14d)            ← from D8 stage tracking
  2 won cards missing $ value           ← from D8 data-quality flag
```
*Why the leaks list:* a funnel nobody acts on is decoration. The list names the specific leads
rotting right now.

### D13 — Full transcript goes to the GHL note
The AI summary **and** the full call transcript are pushed into the GHL contact/opportunity
note. Ratified by the owner after the narrower summary-only option was recommended.

*Recorded trade-off:* transcripts contain caller service addresses and already live in OWEN,
so this widens disclosure to a third-party processor. Accepted deliberately — the team wants
call content readable in the CRM without switching systems.

### D14 — Workiz stays out of scope
Workiz (the field-service platform where jobs are scheduled/dispatched) is **not** integrated.
No live sync, no one-off import, and no Workiz-shaped custom fields in GHL. GHL remains the
sole source of closure and revenue per D8.

*Accepted risk, stated explicitly:* Workiz would hold real invoiced and paid amounts, whereas
GHL's `monetaryValue` is hand-typed and frequently left at 0. **Revenue per lead source will
be understated by however often the team closes a card without entering a value.** D8's
data-quality counter surfaces the size of that gap; it cannot recover the amounts. If the gap
proves large, reopen D14 — Workiz would turn "closed" from a pipeline opinion into a financial
fact.

### D15 — GHL custom fields are created by OWEN, via API
Created as a one-off script against the v2 custom-fields endpoint (mirroring the existing
`ghl_api.list_pipelines()` pattern), then their resolved IDs stored in config:

| Field | Source in OWEN |
|---|---|
| `owen_campaign` | `campaigns.name` |
| `owen_tracking_number` | `numbers.phone_number` |
| `owen_call_id` | `calls.id` (round-trip link) |
| `owen_is_new_caller` | `calls.is_new_for_campaign` |
| `owen_lead_trigger` | which D3 clause qualified it: `job-tag` / `missed` / `long-call` |
| `owen_call_signal` | D17 advisory progress signal, e.g. `quote given (unverified)` |
| `owen_signal_at` | when that signal was last computed |

**STATUS: DONE — created in the live account 2026-07-24.** All seven exist on the
`opportunity` model, `dataType=TEXT`. Field ids (needed by the push code):

```
owen_campaign         LFG2NGPblzA9p03a0p1n   key: opportunity.owen_campaign
owen_tracking_number  5D3NRiXhc1mPXH3iJGhp   key: opportunity.owen_tracking_number
owen_call_id          Gy22E8Pixfs6oz94BLAE   key: opportunity.owen_call_id
owen_is_new_caller    F6wf801Y6oOXDpbWq8KB   key: opportunity.owen_is_new_caller
owen_lead_trigger     Wrns6Xb58rejK7MPlM00   key: opportunity.owen_lead_trigger
owen_call_signal      V9tlPxHg0H3OiyBhNaeL   key: opportunity.owen_call_signal
owen_signal_at        xQuMPOXeWbKEWcZ68vYj   key: opportunity.owen_signal_at
```

All are TEXT for reliability (no date-format coupling). `owen_signal_at` can be promoted to a
DATE field later if sorting on it in GHL becomes useful. Creation is idempotent — the script
skips any field whose name already exists, so it is safe to re-run.

### D16 — OpenPhone access is READ-ONLY, enforced structurally
OWEN issues **GET requests only** against OpenPhone. It never sends a message, places a call,
or writes a contact — anything that could incur a charge on a real customer's number.

*Why structural rather than careful:* in OpenPhone a stray `POST /messages` does not fail a
test, it **texts a real customer and bills for it**. Discipline is not a control. So:

- `providers/openphone_client.py` exposes `_get` and nothing else — there is no `_post`,
  `_put` or `_delete` helper to misuse.
- `_get` refuses action-shaped paths (`/send`, `/dial`, `/create`, …) before the request
  leaves the process. Verified: all three refuse.
- The module docstring states that adding a write method silently removes the guarantee.

If a write is ever genuinely required it belongs in a separate, separately-reviewed module —
never by relaxing this one.

**Status:** `OPENPHONE_API_KEY` is set on the server. `app.scripts.probe_openphone` is written
and ready to confirm the (currently unverified) endpoint paths, auth header form, response
shapes, and whether recordings/transcripts are exposed. It has not been run — it needs the
prod environment where the key lives.

## Verified production state (read-only audit, 2026-07-24)

Measured on prod. Several of these **invalidate assumptions this spec was written on** —
read before building.

### Queue: clear. D10 is no longer a blocker.
```
pending=0   running=0   failed=416   total=27,079
```
The ~11,800 backlog referenced in D10 **is gone**. The 416 failures are historical
`recording_fetch` 404/403s (media already deleted at Twilio/SignalWire — a known, closed
issue). **D10 is downgraded from prerequisite to hygiene**: still architecturally right (a
single serial drainer with no priority will bite again on the next bulk job), but it no longer
gates Phase 1.

### Attribution — far healthier than the raw counts suggest

A first pass showed "641 of 30,576 calls attributed = 2.1%" and read as a crisis. It isn't.
**83% of rows in `calls` are not calls.**

```
calls table                     30,576
  └─ stub rows (no number_id)   25,490   ← artifacts, see below
  └─ REAL calls                  5,086
       ├─ campaign stamped         641   (12.6%)
       └─ fixable by one UPDATE  4,196   ⇒ 4,837 / 5,086 = 95%
```

**The 25,490 stubs are recording-backfill artifacts, not calls.** 25,292 of them have
`started_at = NULL`, `direction = NULL` and no `raw_payload` at all. They were created by
`_ensure_call` inside `ingest_recording_event` — the guard that lets a recording arriving
before its status webhook still land on a row. The historical mirror of 26,845 recordings
therefore minted ~25k bare stubs for calls whose status events were never ingested. They carry
no timestamp, number, or direction and are **unattributable by construction**.

> **Reporting rule:** every query must exclude `number_id IS NULL` (or require
> `started_at IS NOT NULL`). `SELECT count(*) FROM calls` is a meaningless number in this
> database and will overstate volume ~6×.

**Why real calls lack a campaign:** `campaign_id` is stamped onto the call **at ingest** from
the number's then-current campaign. Numbers assigned to campaigns *later* never back-stamped
their existing calls. The numbers themselves are in good shape — **80 of 89 already carry a
campaign** (DTR, GBP, YELP, Craiglist, GD, GBP-GD, Locksmith).

**Fix — APPLIED 2026-07-24 (owner-authorised):**
```sql
UPDATE calls c SET campaign_id = n.campaign_id
FROM numbers n
WHERE c.number_id = n.id AND c.campaign_id IS NULL AND n.campaign_id IS NOT NULL;
```
4,196 rows updated. Attribution on real calls went **645 → 4,841 of 5,090 = 95.1%**.

Revert snapshot (all 4,196 call ids) is on the persistent recordings volume:
`/data/recordings/owen_campaign_backfill_20260724T211457Z.csv` — in-container path; on the
host, `/var/lib/docker/volumes/callmon_recordings/_data/`. Old value was uniformly NULL, so
revert is `UPDATE calls SET campaign_id=NULL WHERE id IN (<ids from that file>)`.

**Campaign distribution after the backfill** (real calls only — stubs excluded):

| Campaign | Calls |
|---|---:|
| GBP | 3,474 |
| DTR | 1,107 |
| *(unattributed)* | 249 |
| Craiglist | 158 |
| YELP | 67 |
| GBP-GD | 26 |
| GD | 6 |
| Locksmith | 3 |

Only **9 low-volume numbers** genuinely lack a campaign and need a human decision:
`+19542814566` (159 calls), `+19412600510` (35), `+16452516222` (32), `+19549062005` (10),
`+19412074600` (5), `+19412573808` (4), `+17547049800` (3), `+19413528002` (1), and one BulkVS
DID. Together ~250 calls — immaterial to reporting.

**D9's decision stands unchanged**, and its rationale is now *stronger* than when written:
historical leads-per-campaign genuinely is one UPDATE away, on the 5,086 rows that are real.

### Analysis pipeline — skipped by design, not broken
```
recordings        26,845   (26,440 on disk)
transcribed           47
transcriptions        89   (53 with speaker segments)
call_analysis         25
calls tagged "job"     5
```
Both engines work — `openai` transcription (89 rows), `minimax` analysis (19 `MiniMax-M2` +
6 fallback), 87 of those transcriptions produced in the last 7 days, so **the chain is live for
new calls**.

The corpus was skipped **deliberately**: `handle_recording_fetch` honours a `skip_transcribe`
payload flag, documented for "a raw historical backfill that only wants the audio mirrored
locally, no transcription/analysis cost". The 26k mirror ran with it set. Nothing is broken —
and per the owner's decision (D18) the historical corpus stays unprocessed.

**Consequence for D3:** the `job` tag exists on **5 calls in the entire database**. The tiered
lead rule's job-tag clause is currently near-dead, and the *missed* / *>30s* clauses carry it
almost alone — which is exactly why D3 was specified with those fallbacks. Spam filtering,
categories and job tagging are effectively unavailable on historical data.

Recording retention (`RECORDING_RETENTION_DAYS=30`) only deletes *transcribed* audio, so the
26k untranscribed files are not being reaped. Disk is nonetheless fine: 41% used, 116 GB free,
recordings 3.2 GB.

### GHL account: verified
- **3 pipelines already exist** (D5 assumed one):

  | Pipeline | id | Stages |
  |---|---|---|
  | Marketing Pipeline | `24pMymooHj5DFu9vSx2V` | New Lead → Contacted → Qualified → Proposal Sent → Negotiation → Closed |
  | Dream Team Roofing AHS | `TRbZj4CJ88qZJqr1TRGA` | New Lead → Inspection → Request Approval (AHS) → Approved-Repair Schedule → Repair in Process → Submit Invoice → Call Back → AHS Upgrades → Submit Invoices |
  | Dream Team Roofing Retail Repairs | `FwTahs2XI0w7D98LQOEk` | New Lead → Contacted → Proposal Sent → Closed |

  **D5 may not need a new pipeline** — "Retail Repairs" already has the right shape.
- **Custom-field scope: GRANTED** (owner updated the PIT, verified 2026-07-24). Was 401, now
  `GET /locations/{id}/customFields` → **200**. D15 unblocked. The account currently has
  **zero custom fields defined**, so all seven in D15 must be created.
- **Scope audit — 7/8 pass:** customFields (all models), contacts search, opportunities
  search, opportunities pipelines, conversations search all return 200. Only
  `GET /locations/{id}` returns 401, which is **irrelevant** — no planned code path reads the
  location object (`ghl_api.py` touches contacts, opportunities, pipelines and notes only).
- **`GET /conversations/search` → 200**, confirming D2's premium-webhook replacement for
  inbound SMS logging is viable.
- **`GET /opportunities/search` → 200**, using snake_case `location_id`. Confirms the D7
  back-sync endpoint. Opportunity fields include `status`, `pipelineStageId`, `monetaryValue`,
  `updatedAt`, **`lastStatusChangeAt`**, **`lastStageChangeAt`**, `customFields`, `source`,
  and a native `attributions` object worth inspecting before finalising D4.

## Build order

**Phase 1 — prove the loop end to end** (nothing else starts until this is trusted)
1. ~~D10 queue concurrency + priority~~ — **deferred**, queue is empty; no longer a prerequisite
2. D4 `ghl_contact_id` / `ghl_opportunity_id` migration + sync cursor in `app_settings`
3. **Match, never create** (D21): qualifying calls → find the EXISTING GHL contact by phone →
   enrich it (and any open opportunity) with the `owen_*` fields. Count the unmatched.
4. Pull: polled status/revenue back (D7, D8)
5. One funnel table in OWEN (D12, first half) — **both close rates**, per D21

D21 reshaped this phase: there is no create path to build, so the push side shrinks to
match-and-patch, but gains a phone-matching step and a "qualified but never entered in GHL"
counter — which is the new leak metric.

**Phase 2** — build the direct-API SMS/email push (D2). Note: no *migration* is needed — the
premium webhook URLs were never set in prod, so nothing is running to replace.

**Phase 3** — Quo/OpenPhone touch ingestion (D11, D11a)
- contact-driven poll (no bulk sweep exists), scoped to open/recent leads
- pull transcripts into `transcriptions.segments` — free STT, no new schema

**Phase 4** — advisory progress signal (D17)
- run the existing `analysis/classification.py` engine over Quo follow-up transcripts
- write `owen_call_signal` / `owen_signal_at` to the GHL card (D15)
- divergence detection: signal moving + GHL card static ⇒ stale-card alert

**Phase 5** — leaks list + full dashboard (D12, second half)

## Open items

All design questions are resolved. What remains is credentials and verification.

1. **Credentials must reach `.env.prod` on the server** (they are prod-only; the local
   `backend/.env` has none of them):
   - `OPENPHONE_API_KEY` — **done**, set on the server by the owner.
   - Confirm the existing `GHL_API_TOKEN` has custom-field **write** scope (D15).
2. **Verify GHL v2 endpoint shapes** against current docs before coding — opportunity
   search/filter-by-update-time, opportunity update, custom-field create, and inbound-message
   logging. Endpoint names here are from working knowledge, **not verified against a live
   account**; this is the first task of Phase 1.
3. ~~Verify the OpenPhone API surface~~ — **DONE, see D11a.** Remaining sub-item: confirm
   whether `call-summaries.jobs` / `nextSteps` populate on a real conversation (the sampled
   call hit voicemail). Needs a call with actual dialogue.
4. **Live queue depth** — confirm whether the ~11,800 backlog still stands (needs prod access).
   Does not block the D10 fix, only sizes it.
5. **Scope the OpenPhone poll set** (consequence of D11a): decide which callers get polled —
   proposed default is those with an open opportunity or activity in the last 30 days, since
   there is no bulk sweep and each contact costs one request.

## Decision log

| # | Decision | Status |
|---|---|---|
| D1 | GHL = workspace, OWEN = analytics brain; two-way | agreed |
| D2 | No premium GHL actions; direct v2 API + PIT only | agreed (cost constraint) |
| D3 | Tiered lead rule (job-tag OR missed OR >30s) | agreed |
| D4 | OWEN owns attribution; GHL IDs are the join | agreed |
| D5 | Separate "Inbound Leads" pipeline | agreed |
| D6 | Opportunity reuse: open AND < 90d | agreed |
| D7 | Back-sync by cursor polling | agreed |
| D8 | `status` authoritative; flag $0 wins | agreed, reaffirmed after D14 |
| D9 | Contacts-only backfill, no historical opportunities | agreed |
| D10 | Queue concurrency + priority | agreed (Phase 1 prerequisite) |
| D11 | OpenPhone = follow-up touches, never leads | agreed |
| D12 | Source funnel + actionable leaks list | agreed |
| D13 | Full transcript to GHL note | agreed (owner override) |
| D14 | Workiz out of scope; revenue understatement accepted | agreed |
| D15 | OWEN creates GHL custom fields via API | **DONE** — 7 fields live, ids recorded |
| D16 | OpenPhone read-only, enforced structurally | agreed, client written + guard verified |
| D17 | GHL decides outcomes; call signals advisory only | agreed |
| D18 | No historical transcription/analysis; go-forward only | agreed |
| D19 | Ad leads use the EXISTING "Retail Repairs" pipeline (`FwTahs2XI0w7D98LQOEk`) — supersedes D5's new-pipeline plan | agreed |
| D20 | Backfill `calls.campaign_id`; exclude stub rows from reporting | **DONE** — 4,196 rows, 95.1% attribution, snapshot kept |
| D21 | **OWEN never creates GHL records — enrichment only.** Supersedes D3/D6/D9 | agreed |

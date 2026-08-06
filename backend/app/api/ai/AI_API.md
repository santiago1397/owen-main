# OWEN AI API

Read-only HTTP access to OWEN for AI agents and integrations.

**Base URL:** `https://api.owen.santiagoproperties.uk`
**Served live at:** `GET /api/ai/docs` (this file) · `GET /api/ai` (machine-readable index)

This file is the single source of truth: the endpoint serves it verbatim from the repository,
so what you read here is what is deployed.

---

## What OWEN is

An ad/campaign **call-attribution** platform for a roofing business in Miami. Ads run against
dedicated tracking phone numbers; callers dial a tracking number and are forwarded to the real
line. OWEN ingests every call, attributes it to the campaign that owns the dialed number,
records and transcribes it, runs an LLM over the transcript, and also ingests SMS and
job-notification emails (American Home Shield work orders arriving via Dispatch), pushing
qualified leads into GoHighLevel.

So the data you can ask about is: **calls**, **leads** (job emails), **messages** (SMS),
**telephony spend**, and **operational errors**.

---

## Authentication

Every request needs an API key, issued in the OWEN UI under **API Keys**.

```
X-OWEN-Key: owen_sk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

`Authorization: Bearer owen_sk_...` is accepted identically, if your client only speaks Bearer.

Keys are **read-only** — nothing in this API can change OWEN's data.

### Scopes

| scope | what it unlocks |
|---|---|
| `read` | Curated metrics: call/lead/message counts, durations, series, pipeline health, schema |
| `content` | Transcripts, AI summaries, SMS bodies, customer names and addresses |
| `sql` | `POST /api/ai/query` — arbitrary read-only SQL |
| `logs` | `GET /api/ai/errors` — captured warnings, dead jobs, failed relays |

`/api/ai/query` requires **both `sql` and `content`**. This is honest rather than pedantic: a
database role that can run `SELECT` can read transcripts and customer addresses, so pretending
`sql` alone were content-free would be a lie told by the scope name.

A 403 always names the exact scope you are missing.

---

## Response shape

Every endpoint returns the same envelope:

```json
{
  "summary": "412 calls Jul 25 - Aug 01 2026 (America/New_York); median 84s, average 121s; 388 answered.",
  "data": { "total_calls": 412, "...": "..." },
  "applied_filters": { "period": "last_7d", "from": "2026-07-25T04:00:00+00:00", "to": "...", "include_junk": false },
  "notes": ["Rows with no started_at are ingestion artifacts...", "Junk calls ... are excluded."]
}
```

- **`summary`** — a sentence you can quote verbatim and still be correct.
- **`data`** — the numbers.
- **`applied_filters`** — exactly what was counted, with the resolved absolute UTC bounds.
- **`notes`** — caveats that apply to *this* answer. **Do not drop these when reporting.** They
  are the difference between a right number and a wrong one.

Errors use the same reflex — they name the problem and tell you what to do:

```json
{ "error": "unknown_campaign", "message": "No campaign named 'AHS'.",
  "hint": "Use one of the known campaign names.", "known_campaigns": ["...", "..."] }
```

---

## Three things that will make you wrong

Read these before quoting any number.

1. **`calls` contains rows that are not calls.** Roughly 25,000 rows have `started_at IS NULL` —
   ingestion artifacts from provider backfills and call-leg correlation. `SELECT count(*) FROM
   calls` returns ~30,000 when real call volume is a small fraction of that. Curated endpoints
   exclude them always and non-optionally. **If you write SQL, you must add
   `WHERE started_at IS NOT NULL` yourself.**

2. **"Junk" calls are excluded by default.** Junk = `duration_seconds <= 13` **or** the call
   never connected (`status IN ('failed','busy','no-answer','canceled')`). These are misdials
   and dropped calls, not leads. OWEN's own dashboard hides them, so this default is what makes
   the API agree with what the business sees. Pass `include_junk=true` to count them; the
   response says so in `notes`.

3. **`is_spam` is dead data.** The LLM spam classifier flagged about 25 calls out of 30,000+.
   It is not a usable quality signal. Use **duration** and **status** instead.

---

## Time

All named periods resolve in the **business timezone, `America/New_York`** — the same timezone
OWEN's dashboard buckets by. Every endpoint accepts either:

- `period=<name>`, or
- `date_from=` / `date_to=` (ISO 8601; naive values are treated as business-local)

| period | meaning |
|---|---|
| `today` | Midnight today (Eastern) until now |
| `yesterday` | The whole previous calendar day |
| `last_24h` / `last_7d` / `last_30d` / `last_90d` | Rolling windows ending now |
| `this_week` / `last_week` | Monday-based weeks |
| `this_month` / `mtd` / `last_month` | Calendar months |
| `ytd` | January 1 until now |
| `all_time` | No lower bound (scans everything — use sparingly) |

Default is `last_7d`. The resolved absolute UTC bounds always come back in `applied_filters`,
so state the window you actually measured rather than the word you passed.

---

## Endpoints

### `GET /api/ai` — index *(any scope)*
Machine-readable list of endpoints, scopes, periods and caveats. Start here if you were given
only a URL and a key.

### `GET /api/ai/docs` — this manual *(any scope)*

### `GET /api/ai/calls/stats` — the workhorse *(scope: `read`)*

| param | meaning |
|---|---|
| `period`, `date_from`, `date_to` | the window |
| `min_duration`, `max_duration` | seconds, **inclusive** — "under 45s" is `max_duration=45` |
| `campaign` | campaign name, case-insensitive; a wrong name returns the valid list |
| `number` | tracking number in E.164, e.g. `+13055559999` |
| `direction` | `inbound` / `outbound` |
| `status` | provider status string |
| `answered` | `true` = the provider reported an answer time |
| `new_callers` | `true` = caller's first-ever call to that campaign |
| `include_junk` | `true` = also count <=13s and never-connected calls |
| `group_by` | `day` (default) · `hour_of_day` · `campaign` · `number` · `status` · `none` |

Returns totals, answered/unanswered, unique callers, new vs returning, duration stats
(average, **median**, p90, min, max, total) and the requested breakdown.

`junk_calls_matching_filters` is how many calls matching *the same filters* were junk — i.e.
what the default view is hiding from you. It carries your campaign/number/duration filters, so
it is directly comparable to `total_calls` rather than being a whole-account figure.

Durations report a real median via `percentile_cont`, not a mean — the average is dragged hard
by the long tail of short calls.

### `GET /api/ai/calls/top-callers` *(scope: `read`)*
`period`, `include_junk`, `limit`. Repeat callers and, usually, the noisiest robocallers.

### `GET /api/ai/calls/categories` *(scope: `read`)*
The LLM's category mix. Only calls that were recorded → transcribed → analyzed appear, so
`analyzed_calls` is well below `total_calls`; the gap is reported, not hidden.

### `GET /api/ai/leads/stats` — new leads *(scope: `read`)*
`period`, `source` (e.g. `dispatch`), `group_by` = `day` · `week` · `source` · `brand` · `none`.

A **lead** is a successfully **parsed** job-notification email.

Mind the three parse outcomes — they are not interchangeable:

| `parse_status` | meaning | is it a problem? |
|---|---|---|
| `parsed` | a work order we fully read | no — it became a lead |
| `cancellation` | the sender cancelled a job it had already dispatched | no — but it's a lead that evaporated, and it's noted on the customer in GHL |
| `ignored` | not a work order at all: a note on an existing job, Dispatch account mail | **no** — there was never a lead in it |
| `failed` | a work order we could **not** read | **yes** — that lead was not relayed |

Do not report `ignored` as a failure. Most Dispatch mail is not a work order, and counting it
as broken makes a healthy parser look broken.

`relay_failed` is different again and is the one that costs money: the email parsed fine and
**GoHighLevel rejected it**, so a real customer never reached the CRM. That is a business item
to chase, not a software error — see `/api/ai/errors`, where these appear as
`source: "lost_lead"`.

`relayed_as_note_on_existing_card` is not a failure: GoHighLevel permits one opportunity per
contact per pipeline, so a repeat customer's second job is attached to their existing card as a
note instead of getting one of its own. It reached the CRM; there is just no separate card to
find for that job number.

### `GET /api/ai/messages/stats` *(scope: `read`)*
SMS/MMS volume. `period`, `direction`, `group_by` = `day` · `direction` · `none`.

### `GET /api/ai/billing/summary` *(scope: `read`)*
Telephony spend from BulkVS's own rated records. `group_by` = `day` · `number` · `kind` ·
`direction` · `none`.

Costs are **per billed leg, not per call**: an inbound call that a flow forwards back out is
billed twice. Recurring DID rental and E911 are not in this feed. Unpriceable legs are recorded
at $0 and counted in `unrated_legs` — when that is non-zero the real total is higher than shown.

### `GET /api/ai/health/pipeline` — "is anything broken?" *(scope: `read`)*
One request covering ingestion freshness, job-queue depth, dead jobs, relay failures, stuck
recordings and telephony reachability.

It returns **two separate lists**, and you should report both:

- **`problems`** — the software or its plumbing is misbehaving. These, and only these, set
  `status` to `degraded`.
- **`needs_attention`** — the software worked and a *business* outcome still needs a person:
  leads GoHighLevel refused (also itemised in `stranded_leads`, with customer names and job
  ids), work orders whose email could not be read. A healthy platform can still have these.

`degraded` means something needs an engineer. An empty `problems` list with a non-empty
`needs_attention` means the system is fine and somebody is losing leads.

### `GET /api/ai/flows/outcomes` — what the IVR did with callers *(scope: `read`)*

| param | meaning |
|---|---|
| `period` / `date_from` / `date_to` | the window, as everywhere else |
| `flow` | restrict to one flow by exact name |

Inbound calls on BulkVS DIDs are answered by a **call flow** (an IVR graph: greeting → menu →
dial / voicemail / hangup). This endpoint reports where callers actually went.

The number to lead with is **`data.dropped`**. A "dropped" call is one whose flow ended in
`unrouted_hangup` — the caller hit a port that was unwired (or errored) on a flow with no
`default_fallback`, so the interpreter hung up on them instead of routing to voicemail or an
operator. **A dropped caller is a `completed` call everywhere else in OWEN**: normal status,
normal CDR, normal dashboard row. This endpoint is the only place the loss is visible.

`menu_outcomes` separates the two ways a menu loses a caller, which look identical in every
other view but need opposite fixes:

- **`port: "timeout"`** — the caller heard the whole prompt and pressed nothing. Usually the
  prompt is long relative to the node's `timeout_s`, or the options don't suit the caller.
- **`port: "invalid"`** — the caller pressed a key the menu doesn't wire up.

`routed` on each row says what happened next: `edge` (a wired target), `fallback` (the flow's
default_fallback), `hangup` (nothing wired and no fallback — the caller was dropped),
`terminal` (a voicemail/hangup node).

Junk/short-call filtering is deliberately **not** applied: a caller the IVR hangs up on after
six seconds is a short call by definition, so excluding them would hide the very thing being
measured. Numbers here will therefore read higher than the dashboard.

> **Not retroactive.** These events are written by the flow interpreter as of this
> instrumentation. A window reaching before it deployed under-counts, and zero dropped calls
> in an old period means *no data*, not *no problem*. The response says so in `notes`.

### `GET /api/ai/flows/calls` — which specific callers *(scope: `read`)*

The drill-down behind the aggregate: individual calls with their node `path`, `ended` reason
and flow duration. `ended=unrouted_hangup` lists exactly the callers who were dropped — use it
to verify a flow fix against real calls instead of trusting an aggregate to move.

### `GET /api/ai/errors` — what is going wrong *(scope: `logs`)*

| param | meaning |
|---|---|
| `since` | `30m` · `6h` · `7d` (default `24h`) |
| `source` | `logs` · `jobs` · `emails` (comma-separated; default all three) |
| `level` | `WARNING` · `ERROR` · `CRITICAL` |
| `service` | `app` · `worker` |
| `linkedid` | only records correlated to one call |
| `limit` | up to 500 |

Unions three places OWEN records failure — captured log records, jobs that died after 5
attempts, and failed email parses/relays — into one time-ordered list.

**Not everything here is a bug.** Check `source`:

- `log` / `job` / `email` (severity `ERROR`/`WARNING`) — something malfunctioned.
- **`lost_lead`** (severity `ACTION_REQUIRED`) — the email parsed correctly and GoHighLevel
  rejected it. Nothing is broken; a named customer simply never reached the CRM. Each carries
  `customer`, `job_id` and a `retry` path. Report these as leads to chase, never as errors.

Dispatch mail classified `ignored` (cancellations, notes, account mail) never appears here at
all — it carries no lead and nothing failed.

Two limits worth stating when you report from this: capture starts at **WARNING** (anything
below exists only in Docker logs on the VPS), and it is **not retroactive** — nothing from
before this feature was deployed is here.

### `GET /api/ai/calls/recent` *(scope: `content`)*
Individual calls with attribution and, by default, the AI's category and summary. `period`,
`min_duration`, `max_duration`, `include_junk`, `with_summary`, `limit`, `offset`.

### `GET /api/ai/calls/{call_id}/transcript` *(scope: `content`)*
Full transcript for one call. `segments` holds speaker-labeled turns (`[Caller]` / `[Operator]`)
for dual-channel recordings, and is `null` for mono ones.

### `GET /api/ai/leads/recent` *(scope: `content`)*
Individual leads with the extracted `fields` (customer name, phone, service address, brand, job
type). `parse_status=failed` is the human-inspect queue — emails that never became leads.

### `GET /api/ai/schema` *(scope: `read`)*
Live introspection of every readable table and column, plus per-table prose for the ones whose
meaning is not obvious, plus the caveats above. Pass `table=calls` for one table. Read this
before writing SQL.

### `POST /api/ai/query` *(scopes: `sql` + `content`)*

```json
{ "sql": "SELECT date_trunc('day', timezone('America/New_York', started_at)) AS d, count(*) FROM calls WHERE started_at IS NOT NULL AND started_at >= now() - interval '30 days' GROUP BY 1 ORDER BY 1", "limit": 200 }
```

Runs as a dedicated `owen_ro` Postgres role with `SELECT` and nothing else, inside a `READ ONLY`
transaction, with a 10-second statement timeout and a row cap (default 200, max 5000) applied by
wrapping your statement — you cannot opt out of it by omitting `LIMIT`.

One statement per request; use a CTE (`WITH`) if you need several steps.

`SELECT` on `users`, `api_keys` and `api_key_usage` is **revoked at the database level** and
cannot be granted. A permission error on those is intentional, not a bug to work around.

Every query is recorded, with its SQL text, against the key that ran it.

---

## Rate limits

60 requests/minute per key; **10/minute** on `/query`. Exceeding either returns `429` with a
`Retry-After` header.

Prefer one wide request over many narrow ones: every endpoint takes a date range and returns a
full series, so a month of daily numbers is one call, not thirty.

---

## Worked examples

Assume `export OWEN=https://api.owen.santiagoproperties.uk` and
`export KEY=owen_sk_...`.

**How many calls did we get last week?**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/calls/stats?period=last_week"
```

**How many calls were under 45 seconds yesterday?**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/calls/stats?period=yesterday&max_duration=45"
```
`data.total_calls` is your answer; `applied_filters` shows the exact window.

**How many calls lasted over two minutes this month, and how many were answered?**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/calls/stats?period=this_month&min_duration=120"
```

**How many new leads from American Home Shield this week?**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/leads/stats?period=this_week&source=dispatch"
```
Check `data.parse_failed` — if non-zero, some leads never reached the CRM.

**Leads per week for the last quarter:**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/leads/stats?period=last_90d&group_by=week"
```

**Which campaign produced the most calls last month?**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/calls/stats?period=last_month&group_by=campaign"
```

**What time of day do people call?**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/calls/stats?period=last_30d&group_by=hour_of_day"
```
Hours are 0-23 in Eastern time, zero-filled.

**How many callers were brand new to the campaign last week?**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/calls/stats?period=last_week&new_callers=true"
```

**How many calls went unanswered yesterday?**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/calls/stats?period=yesterday&answered=false"
```

**Is anything broken right now?**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/health/pipeline"
```
Read `data.status` and `data.problems`.

**What errors happened in the last 6 hours?**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/errors?since=6h"
```

**Only worker errors, excluding warnings:**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/errors?since=24h&service=worker&level=ERROR"
```

**What did people call about this week?**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/calls/categories?period=this_week"
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/calls/recent?period=this_week&min_duration=60&limit=25"
```

**What was said on one call?**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/calls/{call_id}/transcript"
```

**What did we spend on phone service last month, per number?**
```bash
curl -s -H "X-OWEN-Key: $KEY" "$OWEN/api/ai/billing/summary?period=last_month&group_by=number"
```

**Something no endpoint covers — average call duration by weekday:**
```bash
curl -s -X POST -H "X-OWEN-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"sql":"SELECT to_char(timezone('"'"'America/New_York'"'"', started_at), '"'"'Day'"'"') AS weekday, count(*) AS calls, round(avg(duration_seconds)) AS avg_seconds FROM calls WHERE started_at IS NOT NULL AND duration_seconds > 13 AND started_at >= now() - interval '"'"'90 days'"'"' GROUP BY 1 ORDER BY 2 DESC"}' \
  "$OWEN/api/ai/query"
```
Note the mandatory `started_at IS NOT NULL` and the junk filter — you must apply both yourself
in SQL.

---

## The CLI

`cli/owen.py` in this repository wraps every endpoint above, for agents with a shell rather
than an HTTP client.

```bash
export OWEN_API_URL=https://api.owen.santiagoproperties.uk
export OWEN_API_KEY=owen_sk_...

owen docs                                    # this manual
owen calls --period last_week --max-duration 45
owen leads --period this_week
owen health
owen errors --since 6h
owen query "SELECT count(*) FROM calls WHERE started_at IS NOT NULL"
```

Output is JSON by default (predictable to parse); add `--table` for human-readable output.
`owen --help` lists everything.

# OWEN Call Monitoring Platform — Code Map

> A navigational guide to how this project works end to end. Read this first, then
> jump to the file it points you at. For the *why* behind design decisions, see
> [`../ARCHITECTURE.md`](../ARCHITECTURE.md); for the SignalWire-specific saga see
> [`../SIGNALWIRE_CFB_INGESTION.md`](../SIGNALWIRE_CFB_INGESTION.md).

## What this app is

An ad/campaign **call-attribution** tool. Businesses run ads, each ad gets its own
tracking phone number, callers dial that number, the call forwards to the real
business line. OWEN ingests every call (and its recording) from the telephony
providers (Twilio, SignalWire), attributes it to the campaign that owns the dialed
number, transcribes + LLM-analyzes the recording (spam / category / summary), and
serves a dashboard.

**Stack:** React + Vite (frontend) · FastAPI + async SQLAlchemy (backend) ·
PostgreSQL (native on host) · single-replica background worker · deployed via
Docker Compose behind Traefik.

Live at `https://owen.santiagoproperties.uk` (frontend) / `api.` subdomain (backend).

---

## The 10,000-ft data flow

```
                          ┌─────────────────────────────────────────────┐
   Telephony provider     │                  OWEN                        │
   (Twilio / SignalWire)  │                                             │
                          │                                             │
  ① inbound call ────────▶│  webhooks/*  ──┐                            │
     (real-time push)     │                ├─▶ services/ingestion.py    │
                          │  workers/      │   • upsert `calls`         │
  ①' REST poll  ─────────▶│  reconciler.py─┘   • append `call_events`  │
     (every 5 min,        │                    • stamp campaign_id      │
      the PRIMARY path    │                    • is_new_for_campaign    │
      for SignalWire)     │                            │                │
                          │                            ▼                │
  ② recording ready ─────▶│  Postgres `jobs` queue                     │
                          │        │                                    │
                          │        ▼  (worker.py drain loop)            │
                          │   recording_fetch ─▶ transcribe ─▶ analyze  │
                          │   (download .mp3)   (Whisper)     (LLM)     │
                          │        │                │           │       │
                          │        ▼                ▼           ▼       │
                          │   /data/recordings  transcriptions call_    │
                          │   (local disk)                     analysis │
                          │                                             │
                          │  api/*  ◀──── React frontend (30s polling)  │
                          └─────────────────────────────────────────────┘
```

**Two ingestion paths, one code path.** Both live webhooks and the REST-poll
reconciler normalize into the same `NormalizedCallEvent` / `NormalizedRecordingEvent`
and call the same `ingest_*` functions — so backfill is indistinguishable from
real-time. For this SignalWire account the **reconciler is the primary path**
(webhooks via Call Flow Builder are unreliable; see the SignalWire doc).

**Event-sourced core.** `call_events` is the append-only source of truth; `calls`
is a projection rebuilt from it. Everything is idempotent (ON CONFLICT upserts,
unique keys), so retries and duplicate deliveries are safe.

---

## Repository layout

```
owen-main-software/
├── ARCHITECTURE.md            ← agreed design spec + decision log
├── SIGNALWIRE_CFB_INGESTION.md ← why SignalWire needed a special path
├── README.md                  ← phase-by-phase status + setup/deploy
├── docker-compose.prod.yml    ← app + worker + frontend, Traefik labels
├── Makefile                   ← build/up/deploy/create-admin/manage targets
├── scripts/deploy.sh          ← ssh → ff-pull → build → up → healthcheck
├── docs/                      ← you are here
│
├── backend/
│   ├── Dockerfile             ← python:3.12-slim, non-root, uvicorn 4 workers
│   ├── requirements.txt
│   ├── alembic/               ← migrations (4-step linear chain)
│   └── app/
│       ├── main.py            ← FastAPI app, router wiring, /health
│       ├── worker.py          ← worker container entrypoint (drain + scheduler)
│       ├── migrate.py         ← alembic behind pg_advisory_lock
│       ├── db.py              ← async engine, SessionLocal, get_db
│       ├── core/              ← config (settings), security (JWT + HMAC)
│       ├── models/models.py   ← all SQLAlchemy tables
│       ├── schemas/api.py     ← pydantic request/response shapes
│       ├── api/               ← HTTP routers (JWT-authed): calls, dashboard, …
│       ├── webhooks/          ← provider webhook endpoints (signature-authed)
│       ├── providers/         ← per-provider adapters + REST clients
│       ├── services/          ← ingestion, queue, recordings (the core logic)
│       ├── workers/           ← job handlers + reconciler
│       ├── analysis/          ← pluggable transcription + LLM analysis engines
│       └── scripts/           ← create_admin, manage (CLI admin tools)
│
└── frontend/
    ├── Dockerfile             ← node build → nginx:alpine serve on :3333
    ├── nginx.conf             ← SPA fallback + asset caching
    ├── vite.config.ts         ← dev proxy /api,/webhooks → :8888
    └── src/
        ├── main.tsx           ← QueryClient (30s poll) + Router bootstrap
        ├── App.tsx            ← routes + auth-gated Layout
        ├── api.ts             ← fetch wrapper, token storage, endpoint methods
        └── pages/             ← Login, Dashboard, Calls, Numbers, Callers, Settings
```

---

## Backend subsystems

### Core (`app/core/`, `app/db.py`, `app/main.py`, `app/migrate.py`)

| File | Responsibility |
|---|---|
| `main.py` | Builds the `FastAPI` app; `lifespan` runs migrations at startup; adds CORS; includes all `api/*` + `webhooks/*` routers; `GET /health` (runs `SELECT 1`, gates the container healthcheck, no auth). |
| `db.py` | `create_async_engine(database_url)` (asyncpg), `SessionLocal` (`async_sessionmaker`), `Base`, `get_db()` dependency. |
| `migrate.py` | `run_migrations()` — swaps to a sync psycopg2 engine, takes `pg_advisory_lock(0x0CA11)` so only one container migrates, runs Alembic to head. Also re-enables loggers that Alembic's `fileConfig` would otherwise disable (see SignalWire doc infra-bug #1). |
| `core/config.py` | `Settings` (pydantic-settings, reads `.env`). All tunables: DB creds, `SECRET_KEY`/JWT, `BUSINESS_TZ`, provider creds, `RECONCILE_WINDOW_HOURS`, recordings dir/retention, transcription + analysis engine selection + API keys, `GHL_INBOUND_WEBHOOK_URL` (inbound-SMS relay target), `GHL_CALL_WEBHOOK_URL` + `GHL_CALL_RELAY_DELAY_SECONDS`/`GHL_CALL_RELAY_MAX_WAIT_SECONDS` (completed-call relay target + timing). Singleton `settings`. |
| `core/security.py` | argon2 password hashing; JWT create/decode (access / refresh / **playback** token types); webhook HMAC-SHA1 verifiers (`verify_twilio_signature`, `verify_signalwire_signature` — kept separate per provider). |

### Data model (`app/models/models.py`)

The whole schema in one file. Tables (PK type in parens):

- **`providers`** (int) — `name` unique, `account_ref`. FK target for numbers/calls.
- **`campaigns`** (uuid) — `name`, `source`, `active`. An ad campaign.
- **`numbers`** (uuid) — a tracking phone number. `provider_id`, `campaign_id` (nullable FK), `phone_number` (E.164), `friendly_name`, `forwards_to`, `active`. Unique `(provider_id, phone_number)`. **One number = one campaign, never recycled.**
- **`callers`** (uuid) — a distinct caller phone. Global `first_seen_at`/`last_seen_at`/`total_calls`, `spam_score`, manual `label` override.
- **`calls`** (uuid) — the projection everything reads. `provider_id`, `provider_call_sid`, `number_id`, `caller_id`, `campaign_id` (all stamped at ingest), `direction`, `status`, `status_rank`, timestamps, `duration_seconds`, `forwarded_to`, `is_new_for_campaign`, `raw_payload`, `relayed_to_ghl`/`relayed_at` (completed-call GHL relay-once guard). Unique `(provider_id, provider_call_sid)` = idempotency key. Composite indexes on `(number_id, started_at)` and `(campaign_id, started_at)`.
- **`call_events`** (uuid) — append-only truth. `call_id`, `event_type`, `provider_sequence`, `payload`. Unique `(call_id, event_type, provider_sequence)` = dedup key.
- **`recordings`** (uuid) — `call_id`, `provider_recording_sid` (unique = idempotency), `status`, `storage_path`, `provider_url`, `downloaded_at`, `transcribed` (gates retention deletion).
- **`transcriptions`** (uuid) — `call_id`, `recording_id`, `engine`, `text`, `language`, `confidence`, `words`, `segments` (speaker-labeled `{speaker,start,end,text}` list from dual-channel recordings; NULL for mono).
- **`call_analysis`** (uuid) — `call_id` (unique), `is_spam`, `spam_confidence`, `category`, `tags`, `summary`, `model`, plus human `category_override` / `is_spam_override` (human wins).
- **`messages`** (uuid) — inbound SMS on a tracking number. `provider_id`, `provider_message_sid` (unique = idempotency), `number_id`/`caller_id`/`campaign_id` (attributed like a call), `direction`, `from_number`, `to_number`, `body`, `status`, `num_media`, `media_urls`, `relayed_to_ghl`/`relayed_at` (GHL relay-once guard), `raw_payload`, `received_at`. Composite indexes on `(number_id, received_at)` and `(campaign_id, received_at)`.
- **`jobs`** (uuid) — durable queue. `type`, `payload`, `status`, `attempts`, `last_error`, `run_after`, `locked_at`.
- **`users`** (uuid) — `email` unique, `password_hash` (argon2), `role`, `active`.

Migrations live in `backend/alembic/versions/` as a 7-step linear chain (initial
schema → composite call indexes → recording-sid unique → transcriptions + analysis →
messages → transcription segments → call GHL-relay flags).

### Ingestion — the correctness core (`app/services/ingestion.py`)

`ingest_status_event(db, provider_name, evt)` is the heart of the app. For each
normalized call event it:

1. Resolves the `Provider` (get-or-create).
2. Looks up the `Number` by `(provider_id, to_number)` → its `campaign_id`. (No match = call stays unattributed, logs a warning.)
3. Get-or-creates the `Caller` from `from_number`.
4. Computes `is_new_for_campaign` at first sight (any prior call for this `(caller, campaign)`?).
5. **Upserts** `calls` via `INSERT … ON CONFLICT (provider_id, provider_call_sid) DO NOTHING`.
6. **Atomic forward-only status advance**: `UPDATE … WHERE status_rank < new_rank` — late/out-of-order events can't regress status, and `COALESCE`-style fills back-fill attribution onto stub rows created by an earlier recording event.
7. **Appends** to `call_events` with `ON CONFLICT (call_id, event_type, provider_sequence) DO NOTHING`.
8. Increments `caller.total_calls` exactly once per call.

`ingest_recording_event` (in `services/recordings.py`) is the analogous idempotent
upsert for recordings; it `_ensure_call` first so a recording arriving before its
status webhook still lands on the right row.

`ingest_message_event` (in `services/messages.py`) is the analogous idempotent upsert
for inbound SMS: resolves the `Number` by `(provider_id, to_number)` for campaign
attribution (reusing `_get_or_create_provider`/`_get_or_create_caller`), then upserts
`messages` `ON CONFLICT (provider_message_sid)`. The `/message` route enqueues a
`message_relay_ghl` job to forward it to GoHighLevel.

### Webhooks (`app/webhooks/`) — real-time push ingestion

- `common.py` — `build_router(adapter, provider, signature_headers)` factory. Reconstructs the public URL behind Traefik, verifies the signature (or SignalWire CFB HTTP Basic Auth via `SIGNALWIRE_CFB_WEBHOOK_SECRET`), parses body → adapter → `ingest_*`, returns 200 fast (slow work is enqueued). `POST /status` (on a terminal call, if `GHL_CALL_WEBHOOK_URL` is set, enqueues a delayed `call_relay_ghl`), `POST /recording`, and `POST /message` (inbound SMS → `ingest_message_event` → enqueue `message_relay_ghl`).
- `twilio.py` / `signalwire.py` — one-liners that instantiate the router with the right adapter + signature header names.

### Providers (`app/providers/`) — per-provider translation

| File | Role |
|---|---|
| `base.py` | `NormalizedCallEvent` / `NormalizedRecordingEvent` / `NormalizedMessageEvent` dataclasses, `ProviderAdapter` protocol, `STATUS_RANK` (the monotonic status ordering). |
| `cxml.py` | Shared normalization of Twilio/SignalWire cXML REST resources → `NormalizedCallEvent` (used by reconciliation). |
| `twilio.py` | `TwilioAdapter` — parses Twilio webhook form fields (`CallSid`, `CallStatus`, `From`, `To`, …), verifies signature. |
| `signalwire.py` | `SignalWireAdapter(TwilioAdapter)` — adds a **native Relay/Calling** parser for CFB's `calling.call.state` events (single-quoted Python-repr `params`, `parent.call_id` leg correlation, `_tracking_number` override). |
| `twilio_client.py` | REST reconciliation client: `fetch_recent_calls`, `delete_recording`. |
| `signalwire_client.py` | REST client. **The modern Voice API path** (`fetch_recent_calls_voice_logs`, `fetch_recordings_via_voice_logs`) is what actually works for this account; classic Compatibility-API functions kept unused as fallback. |
| `ghl_client.py` | GoHighLevel inbound relays (shared `_post` helper): `post_inbound_message(payload)` → `GHL_INBOUND_WEBHOOK_URL` (inbound SMS); `post_call_summary(payload)` → `GHL_CALL_WEBHOOK_URL` (completed-call summary + AI analysis). Plain JSON Workflow triggers, no auth. No-op when the URL is unset. Inbound-only — never sends outbound SMS. |

### Background worker (`app/worker.py`, `app/workers/`, `app/services/queue.py`)

- `worker.py` — the worker container's entrypoint. Runs two things in one asyncio process: a **drain loop** (`claim_one` → dispatch to handler → complete/fail) and an **APScheduler** with `reconcile_recent` (every 5 min) + `retention_sweep` (every 6 hrs).
- `services/queue.py` — the Postgres-backed job queue. `enqueue`, `claim_one` (`UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED)` so concurrent drainers never collide), `complete`, `fail` (linear backoff `30s × attempts`, up to `MAX_ATTEMPTS=5` then dead).
- `workers/handlers.py` — the pipeline. `HANDLERS = {recording_fetch, transcribe, analyze, message_relay_ghl, call_relay_ghl}`. The recording pipeline enqueues the next stage on success; `message_relay_ghl` POSTs the message to GHL (`ghl_client`) and sets `relayed_to_ghl`/`relayed_at` (raises to retry on failure); `call_relay_ghl` POSTs a completed-call summary (attribution + new/returning + spam/category/summary via override precedence + transcript), re-deferring while a recording's analysis is still pending (bounded by `GHL_CALL_RELAY_MAX_WAIT_SECONDS`), and sets `calls.relayed_to_ghl`/`relayed_at`:
  - **`recording_fetch`** — streams the provider media to `/data/recordings/{sid}.mp3` (atomic `.part` → `os.replace`), optionally deletes the remote copy, enqueues `transcribe`.
  - **`transcribe`** — runs the configured `TranscriptionEngine`, writes a `Transcription`, sets `recording.transcribed=True` (now retention may delete the audio), enqueues `analyze`. **Dual-channel path:** if the recording is stereo (ffprobe) and `STEREO_TRANSCRIPTION_ENABLED`, ffmpeg splits it into two mono legs, each is transcribed with segment timestamps (whisper-1 → `transcribe_segmented`, hallucination-filtered), and `audio.merge_channels` interleaves them into a `[Caller]`/`[Operator]`-labeled transcript + `segments`. Probe/split failure degrades to the mono path; splits are temp files (cleaned in `finally`).
  - **`analyze`** — runs the configured `AnalysisEngine`, upserts `CallAnalysis`, updates the caller's `spam_score`.
- `workers/reconciler.py` — `reconcile_recent(window_hours)` polls each provider's REST API (per-provider try/except isolation), feeds inbound legs through the same `ingest_*` code path, enqueues `recording_fetch` for any recording not yet on disk, and enqueues `call_relay_ghl` for backfilled terminal calls (so webhook-missed calls still reach GHL).

### Analysis engines (`app/analysis/`) — pluggable

Selected at runtime by `settings.TRANSCRIPTION_ENGINE` / `settings.ANALYSIS_ENGINE`.

- `transcription.py` — `TranscriptionEngine` protocol (`transcribe` + `transcribe_segmented` for timestamped output). `dummy` (canned, offline/tests) · `openai` (mono via `OPENAI_TRANSCRIBE_MODEL`=gpt-4o-transcribe; stereo legs via whisper-1 for segment timestamps). **Prod uses `openai`.**
- `classification.py` — `AnalysisEngine` protocol; controlled `CATEGORIES` vocab. `dummy` (keyword heuristic) · `claude` (Anthropic Messages API, tool-use for structured output) · `minimax` (OpenAI-compatible function calling). **Prod uses `minimax`.**

### HTTP API (`app/api/`) — all JWT-authed except where noted

| Router | Endpoints |
|---|---|
| `auth.py` | `POST /api/auth/login` (OAuth2 form, no auth) · `POST /api/auth/refresh` · `GET /api/auth/me` |
| `calls.py` | `GET /api/calls` (filter by provider/number/campaign/caller/status/date + `include_short` + pagination) · `GET /api/calls/{id}` (events timeline + recordings + transcript + analysis) · `PATCH /api/calls/{id}/analysis` (human override) |
| `callers.py` | `GET /api/callers` (filterable) · `PATCH /api/callers/{id}` (manual label) |
| `numbers.py` | `GET /api/numbers` (per-number volume + last call) |
| `dashboard.py` | `GET /api/dashboard/summary?range=` (totals, new-vs-returning both flavors, avg duration, by-campaign/number, Eastern-time daily series, top callers) |
| `recordings.py` | `GET /api/recordings/{id}/play` (JWT → short-lived playback token) · `GET /api/recordings/stream?token=` (**no JWT** — token-authed, streams the file so `<audio>` works) |
| `settings.py` | `GET /api/settings` (masked creds, webhook URLs to paste into providers, active engines, categories) |
| `deps.py` | `current_user` JWT dependency; `SHORT_CALL_MAX_DURATION_SECONDS=1` (≤1s misdials hidden by default). |

`calls`/`dashboard` read `calls` left-joined to `call_analysis`, using
`coalesce(override, model_value)` so human overrides win.

---

## Frontend (`frontend/src/`)

React 18 + Vite 5 + TypeScript SPA. TanStack Query v5 (global **30s polling**,
`refetchOnWindowFocus: false`), React Router v6, Recharts.

- `main.tsx` — bootstraps `QueryClientProvider` + `BrowserRouter`.
- `App.tsx` — routes; `Protected` wrapper renders `Layout` (sidebar nav + logout) only if a token exists, else redirects to `/login`.
- `api.ts` — token in `localStorage['callmon_token']`; `request()` wrapper injects `Authorization: Bearer`, hard-redirects to `/login` on 401; typed methods for every backend endpoint; `API_BASE` from `VITE_API_BASE` (baked at build time).
- Pages:
  - **`Login`** — OAuth2 password form → stores token.
  - **`Dashboard`** — range selector; 5 stat cards; Recharts line (calls/day) + donut (new vs returning) + bar (by campaign); top-callers table.
  - **`Calls`** — filterable table (When, Caller, Campaign, Status, Dur, Flags); row click opens `CallDrawer` (details, recording player via signed playback URL, analysis + override select, events timeline).
  - **`Numbers`** — read-only table: Number, Friendly, Provider, Campaign, Forwards to, Calls, Last call.
  - **`Callers`** — filterable; inline `label` edit via mutation.
  - **`Settings`** — provider config status + copyable webhook URLs + active engines.

### Serve/build

`Dockerfile` builds the Vite bundle (`VITE_API_BASE=https://api.${APP_DOMAIN}`
baked in) and serves it with nginx on `:3333`. `nginx.conf` does SPA fallback
(`try_files … /index.html`) and 1-year immutable caching for `/assets/`.

---

## Deploy & ops

- **Topology** (`docker-compose.prod.yml`): three containers — `app` (uvicorn, Traefik `api.${APP_DOMAIN}`), `worker` (single replica, no inbound routing), `frontend` (nginx, Traefik `${APP_DOMAIN}`). Postgres is **native on the host**, reached via `host.docker.internal` + `extra_hosts: host-gateway`. `expose:` only, never `ports:`. Recordings on a named volume.
- **Deploy**: `make deploy` → `scripts/deploy.sh` → ssh to VPS (`callmon` alias, or `dispatch` per the deploy memo), `git merge --ff-only origin/main`, rebuild, `up -d`, poll `/health`. Migrations self-apply at startup behind the advisory lock.
- **Server path**: `/opt/santiagoproperties/owen-main`. Secrets in `.env.prod` (chmod 600, git-ignored).
- **Gotcha**: any bare `docker compose` command **must** include `--env-file .env.prod` or Traefik's `Host()` labels resolve empty and break API routing. The Makefile/`deploy.sh` always pass it.
- **Admin CLI**: `make create-admin e=… p=…`; `make manage args='add-campaign …'` / `add-number …` / `list` / `reconcile-now`.

---

## Where to start for common tasks

| I want to… | Start in |
|---|---|
| Change what the calls list/table shows | `api/calls.py` (`GET /api/calls`) + `frontend/src/pages/Calls.tsx` + `schemas/api.py` (`CallListItem`) |
| Change how a call is attributed to a campaign | `services/ingestion.py` (`ingest_status_event`, number lookup) |
| Add/adjust a dashboard metric | `api/dashboard.py` + `frontend/src/pages/Dashboard.tsx` |
| Support a new telephony provider | new `providers/<x>.py` adapter + `<x>_client.py`, wire in `webhooks/` + `workers/reconciler.py` |
| Swap the transcription or LLM engine | `analysis/transcription.py` / `analysis/classification.py` + `.env` engine setting |
| Add a background job type | `services/queue.py` (enqueue) + `workers/handlers.py` (`HANDLERS`) |
| Change the DB schema | new Alembic migration in `backend/alembic/versions/` + `models/models.py` |
| Register a tracking number | `make manage args='add-number …'` (see README) |

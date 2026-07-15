# Call Monitoring Platform

Ad/campaign call-attribution tool. See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the full agreed spec.

## Status: Phases 1 & 2 complete and tested

**Phase 1 — Twilio ingestion + auth foundation**
- FastAPI `app` (HTTP) + single-replica `worker` (queue drainer + APScheduler).
- Event-sourced, idempotent Twilio webhook ingestion (`/webhooks/twilio/status|recording`).
- JWT auth (`/api/auth/login|refresh|me`), argon2 password hashing.
- Postgres-backed job queue; **reconciliation** (real Twilio `GET /Calls` backfill, hourly).
- Alembic migrations run at startup behind a `pg_advisory_lock`.

**Phase 2 — Recording pipeline**
- `recording_fetch` worker: streams the provider's media into local disk (atomic write, idempotent).
- **Transcription-gated retention** sweep (6-hourly): deletes audio older than
  `RECORDING_RETENTION_DAYS` **only when `transcribed=True`**; keeps the row + (future) transcript.
- Signed playback: `GET /api/recordings/{id}/play` (JWT) → short-lived token →
  `GET /api/recordings/stream?token=` streams the file. Raw paths never exposed.

**Phase 3 — SignalWire adapter**
- `/webhooks/signalwire/status|recording`, own signature verifier (SignalWire token, separate
  from Twilio) via a shared `build_router` — parsing reused from the Twilio adapter (cXML-compatible).
- Reconciliation now pulls both Twilio and SignalWire Compatibility APIs.

**Phase 4 — Core read API** (React UI still pending)
- `GET /api/calls` (filter by provider/number/campaign/caller/status/date + pagination),
  `GET /api/calls/{id}` (events timeline + recordings), `GET /api/numbers` (per-number volume),
  `GET /api/callers` (filterable), `GET /api/dashboard/summary?range=` (totals, both new-vs-returning
  flavors, avg duration, by-campaign/number, **Eastern-time daily series**, top callers). All JWT-authed.

**Phase 5 — Overrides + settings**
- `PATCH /api/callers/{id}` (manual label), `PATCH /api/calls/{id}/analysis` (category/spam
  override — human wins over the model), `GET /api/settings` (masked creds + webhook URLs + engines).

**Phase 6 — Transcription + LLM analysis**
- Pluggable `TranscriptionEngine` (`dummy` offline default, `openai` Whisper) and `AnalysisEngine`
  (`dummy` heuristic, `claude` Haiku with tool-use structured output).
- Worker chain `recording_fetch → transcribe → analyze`; analysis = spam + controlled category +
  free tags + summary, stored per-call; retention deletes audio only after `transcribed=True`.

**Frontend (React + Vite + TanStack Query + Recharts)** — Login, Dashboard (stat cards, daily
line, campaign bar, new/returning donut, top callers), Calls (filterable table + detail drawer
with timeline, audio player, analysis + override), Numbers, Callers (inline label edit), Settings
(copyable webhook URLs). Builds clean via `npm run build`.

Tests (all green against a live server + Postgres — **62 checks**):
`smoke_live` (15), `smoke_phase2` (10), `handler_download` (5), `smoke_phase34` (16), `smoke_phase56` (16).

Optional / not built: real-time WebSockets and alerting (Phase 7 extras — polling covers the need).

## First-time local setup

```bash
cd backend
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt

# Point at a local Postgres (edit .env or export POSTGRES_* vars), then:
alembic revision --autogenerate -m "initial schema"     # creates the first migration
alembic upgrade head

uvicorn app.main:app --reload --port 8888                # API
python -m app.worker                                     # worker (separate shell)
python -m app.scripts.create_admin admin@example.com 'strong-pass'

# register a tracking number so inbound calls attribute to a campaign
python -m app.scripts.manage add-campaign --name "CL Ads 2" --source craigslist
python -m app.scripts.manage add-number --phone +13055559999 --campaign "CL Ads 2" \
    --friendly "CL Ads 2" --forwards-to +13055550000
python -m app.scripts.manage list

# end-to-end smoke test against a running server (port 8899)
python -m tests.smoke_live
```

Reconciliation (backfill of webhook-missed calls) activates automatically once
`TWILIO_ACCOUNT_SID` + `TWILIO_AUTH_TOKEN` are set; the worker runs it hourly.

> The `versions/` folder ships empty — generate the initial migration once (command above)
> and commit it. After that, `run_migrations()` (startup) applies it automatically.

## Deploy (VPS — see SERVER_SETUP.md)

```bash
cp .env.prod.example .env.prod   # on the server; fill secrets; chmod 600
make deploy                       # ff-only pull, build, up, healthcheck
```

### Server deploy checklist (Phase 1, backend only)

1. **GitHub:** create the repo, `git init && git add -A && git commit && git push -u origin main`
   (this dir is not yet a git repo).
2. **Postgres on the VPS:** `sudo -u postgres createuser callmon` / `createdb -O callmon callmon`,
   set a password, and add the project bridge subnet to `pg_hba.conf` (NOT just `172.17/16` —
   see the `vps-deployment-convention` memo).
3. **DNS:** point **both** `api.<APP_DOMAIN>` and `app.<APP_DOMAIN>` at the server before first
   deploy (ACME needs them). Frontend is served at `app.<APP_DOMAIN>`, API at `api.<APP_DOMAIN>`.
   Set `CORS_ORIGINS=https://app.<APP_DOMAIN>` in `.env.prod`.
4. **SSH alias `callmon`** in your local `~/.ssh/config`.
5. **On the server:** clone to `/opt/owen/callmon`, `cp .env.prod.example .env.prod`, fill secrets
   (`SECRET_KEY`=`openssl rand -hex 32`, DB password, `TWILIO_ACCOUNT_SID`/`TWILIO_AUTH_TOKEN`,
   `APP_DOMAIN`), `chmod 600 .env.prod`.
6. `make deploy` → migrations self-apply at startup; `/health` gates readiness.
7. `make create-admin e=you@domain p='...'`, then `make ... manage add-campaign/add-number`.
8. In Twilio console, set the number's status + recording callbacks to
   `https://api.<APP_DOMAIN>/webhooks/twilio/status` and `/recording`.
9. Place a real call → confirm rows in `calls` + `call_events`.

## Layout

```
backend/app/
  main.py            FastAPI app + /health
  worker.py          worker container entrypoint
  migrate.py         alembic-behind-advisory-lock
  core/              config, security (JWT + webhook sig)
  models/            SQLAlchemy schema
  providers/         base + twilio adapter (signalwire = Phase 3)
  webhooks/          twilio webhook endpoints
  services/          ingestion (event-sourced), queue
  workers/           job handlers, reconciler
  api/               auth (calls/numbers = Phase 4)
```

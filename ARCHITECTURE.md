# Call Monitoring Platform — Architecture (agreed spec)

**Stack:** React (frontend) · FastAPI (backend) · PostgreSQL (native on host)
**Providers:** Twilio first, SignalWire second (extensible)
**Core goal:** Ad/campaign **attribution** — centralize call events, recordings, transcripts
and derived analytics (new-vs-returning, transcript-based spam/tagging, per-number/campaign
volume) from multiple telephony providers into one dashboard.

> This supersedes the original build plan. It records decisions made during design review.

---

## Key decisions

| # | Area | Decision |
|---|---|---|
| 1 | Purpose | **Attribution**, not just logging. `campaigns` table (`name`, `source`, `active`); `numbers.campaign_id` FK; **stamp `campaign_id` onto each `calls` row at ingest** so history freezes and every chart is a flat `GROUP BY`. |
| 2 | Numbers | One number = one campaign, never recycled. |
| 3 | New vs returning | Store **both**: global `callers.first_seen_at` + per-campaign `is_new_for_campaign` on each call. |
| 4 | Transcription | Deferred, **pluggable `TranscriptionEngine`** (local/cloud swappable). Cloud is the realistic default (shared VPS can't run Whisper well). New `transcriptions` table. |
| 5 | Spam + tags | **One LLM analysis job over the transcript** (Claude Haiku, structured output) → `{is_spam, spam_confidence, category (controlled enum), tags (free jsonb), summary}`. Per-call. `callers.label` = manual override. No phone-reputation API. |
| 6 | Ingestion correctness | `call_events` append-only = source of truth; `calls` = projection. Unique `(provider_id, provider_call_sid)`, `ON CONFLICT` upserts, atomic status-rank advance, dedup'd counters. **Reconciliation job (pull provider API) in Phase 1.** |
| 7 | Job queue | **Postgres-backed** (`jobs` table, `FOR UPDATE SKIP LOCKED`). No Redis/Celery. |
| 8 | Workers | Dedicated **single-replica `worker` container** (queue drainer + APScheduler singleton), separate from `app`. |
| 9 | Recordings storage | **Local disk**, automated + **transcription-gated** retention (`delete WHERE age>N AND transcribed`). Disk-monitoring guardrail; keep DB dir separate from recordings dir. |
| 10 | Timezone | `America/New_York`, weeks start Monday. Store UTC, `AT TIME ZONE` in aggregations. |
| 11 | Auth | JWT + `users` table (argon2/bcrypt), **in Phase 1**. `/webhooks/*` stays separate public + signature-verified. |
| 12 | Providers | **Twilio-only vertical slice first**, SignalWire 2nd. `verify_signature` + `download_recording` are per-provider — never shared. |
| 13 | Dashboard | Polling (React Query ~30s) + on-the-fly `GROUP BY`. **No WebSockets.** Queries the normalized `calls` table (provider-agnostic). |
| 14 | Deploy | Conforms to the Traefik/Docker convention in `../../santiago/SERVER_SETUP.md`: native host Postgres (dedicated role+db; **fix `pg_hba` for the project bridge subnet, not just 172.17/16**), `traefik-public`, `expose:` only, `make deploy`, `/health`, resource limits. One domain + `app.` / `api.` subdomains. |
| 15 | Migrations | Alembic at startup behind `pg_advisory_lock`; additive/backward-compatible only. |
| 16 | Backups | Same-VPS `pg_dump` only — **accepted risk**: irreplaceable transcripts/event-log lost if VPS dies. |
| 17 | Legal | Call-recording consent is state-dependent (FL = all-party). Add a recording notice before connect. Flagged, not yet solved. |

---

## Data model (deltas from the original plan)

- **`campaigns`**: `id`, `name`, `source` (e.g. craigslist/facebook), `active`, `created_at`.
- **`numbers`**: add `campaign_id` FK.
- **`calls`**: add `campaign_id` (stamped at ingest), `is_new_for_campaign` bool. `call_events` is the
  append-only truth; `calls` is a rebuilt projection. Status-rank guard on updates.
- **`callers`**: keep global `first_seen_at`/`last_seen_at`/`total_calls`; `label` = manual override.
- **`transcriptions`**: `id`, `call_id`, `engine`, `text`, `language`, `confidence`, `words` jsonb, `status`, `created_at`.
- **`call_analysis`** (LLM): `id`, `call_id`, `is_spam`, `spam_confidence`, `category`, `tags` jsonb,
  `summary`, `model`, `analyzed_at`.
- **`jobs`**: durable queue (`type`, `payload`, `status`, `attempts`, `run_after`, `locked_at`).
- **`users`**: auth (`email`, `password_hash`, `role`).
- **Indexes:** `calls(number_id, started_at)`, `calls(campaign_id, started_at)`, `calls(caller_id)`,
  `callers(phone_number)`, `recordings(call_id)`, unique `calls(provider_id, provider_call_sid)`.

---

## Container topology (per SERVER_SETUP.md convention)

- `app` — FastAPI HTTP only (uvicorn). Routes: `/api/*` (JWT), `/webhooks/*` (public, signed), `/health`.
- `worker` — **single replica**, `command: python -m app.worker`; queue drainer + APScheduler
  (retention, reconciliation). Tiny resource limits.
- Postgres — native on host, reached via `host.docker.internal` + `extra_hosts: host-gateway`.
- Both containers migrate at startup behind a `pg_advisory_lock` (one wins, other waits).
- Traefik routers: `callmon-web` (frontend host), `callmon-api` (`api.<domain>`). `expose:`, never `ports:`.

---

## Build phases

1. **Twilio ingestion + auth foundation** — schema + Alembic, core tables, Postgres `jobs` queue,
   Twilio webhooks (signature-verified) with event-sourced upserts, reconciliation job, JWT auth,
   `/health`, deploy skeleton (app + worker behind Traefik). Prove one real Twilio call end-to-end.
2. **Recording pipeline** — `recording_fetcher` → local disk, transcription-gated retention, signed playback.
3. **SignalWire adapter** — second provider against the proven pattern.
4. **Core API + minimal UI** — `/api/calls`, `/api/numbers`, React calls-log + numbers tables (no charts).
5. **Analytics** — new-vs-returning (both flavors), dashboard summary (Eastern-time buckets), Recharts.
6. **Transcription + LLM analysis** — pluggable transcriber + Claude Haiku classification → spam/category/tags/summary.
7. **Polish** — manual labeling/category override, settings page, optional alerting.

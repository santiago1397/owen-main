# Asterisk as a provider in the event-sourced data model

Type: grilling
Status: resolved (2026-07-22 — ADR below; additive, no regression to Twilio/SignalWire paths)
Assignee: svillahermosa
Blocked by: 02, 04

## Question

How does Asterisk-controlled telephony fit the existing `call_events` (truth) → `calls` (projection) model
without breaking the provider abstraction that Twilio/SignalWire use?

- Is Asterisk a new row in `providers`, and what is its `provider_call_sid` analogue (ARI `channel.id` /
  Stasis app id / Linkedid)?
- Which **ARI events** map to which `call_events` (StasisStart, ChannelStateChange, recording events,
  BridgeEnter, StasisEnd) and how do we preserve the atomic status-rank advance + dedup guarantees?
- Where do **recordings** produced by ARI land vs. the existing local-disk + transcription-gated retention path
  (reuse it)?
- How does **reconciliation** work without a Twilio-style REST "list calls" — from Asterisk CDR? ARI history?
- Do outbound + AI-agent calls use the same `calls` shape, and how are attribution/`campaign_id` stamped for
  numbers that now have live flows?

Use `/grilling` + `/domain-modeling`; record as an ADR-style decision.

## Answer

**Decision (ADR): Asterisk is a third provider feeding the SAME event-sourced `call_events` → `calls`
projection. Everything is additive — a new adapter, a new reconciler branch, a new worker task, a `.wav`
recording path. The live Twilio/SignalWire webhook, projection, recording, and GHL-relay paths are untouched.**

Grounding refs: `backend/app/models/models.py` (`Call` 79-111, `CallEvent` 114-129, `Provider` 35-40,
`Recording` 132-143); `backend/app/services/ingestion.py` (`ingest_status_event` 52-223); `backend/app/
providers/base.py` (`STATUS_RANK` 14-24, `ProviderAdapter` protocol 71-77); `backend/app/providers/
signalwire.py` (native-state→status mapping pattern 18-32); `backend/app/workers/reconciler.py`.

### 1. Call identity
- **New `providers` row `name="asterisk"`** (single on-box instance; `account_ref` = ARI app name `owen`).
  Get-or-created by name exactly like Twilio/SignalWire.
- **`provider_call_sid = Linkedid`** — stable across every channel/leg of one logical call (inbound leg,
  forwarded leg, agent leg, operator/WebRTC leg), so they all collapse into **one `calls` row** via the
  existing unique `(provider_id, provider_call_sid)` key — mirroring one Twilio `CallSid` → one row.
- Per-leg `channel.id` is stored in `call_events.payload`, not used as the call key.

### 2. Ingestion entry point
- ARI has **no signed webhook** — we hold a persistent **ARI WebSocket to `127.0.0.1:8088` (app `owen`)** and
  both receive events and drive the call (answer/play/bridge/record via ARI REST).
- The **ARI consumer runs as a task inside the single-replica `worker` container** (already hosts APScheduler
  + queue drainer; single replica ⇒ exactly one WebSocket, no duplicate-event race). Auto-connect + reconnect.
- Each ARI event → `AsteriskAdapter.parse_*` → the **same `ingest_status_event`** projection. The atomic
  forward-only status advance (`WHERE status_rank < new_rank`) and natural-key dedup are preserved for free.
- `AsteriskAdapter` implements the `ProviderAdapter` protocol; **`verify_signature` is a no-op/True**
  (localhost-trusted, never exposed publicly).
- Tradeoff flagged: couples live call control to the worker process; a worker restart mid-call can drop
  in-flight ARI-driven calls — which is exactly why §5 (CDR reconciliation) exists.

### 3. ARI event → status mapping
`_ARI_TO_STATUS` table in `AsteriskAdapter`, mapping ARI channel lifecycle into the existing Twilio-CallStatus
vocabulary (same pattern SignalWire uses):

| ARI event | condition | internal `status` (rank) |
|---|---|---|
| `StasisStart` (entry channel) | inbound | `ringing` (2) |
| `ChannelStateChange` | `Ring`/`Ringing` | `ringing` (2) |
| `ChannelStateChange` | `Up` | `in-progress` (3) → set `answered_at` |
| `ChannelDestroyed`/`StasisEnd` | cause 16 & was answered | `completed` (4) |
| `ChannelDestroyed` | cause 17 | `busy` (4) |
| `ChannelDestroyed` | cause 18/19, never answered | `no-answer` (4) |
| `ChannelDestroyed` | other causes | `failed` (4) |

- **Dedup key** `provider_sequence = "{Linkedid}:{status}"` (mirrors Twilio's `"{CallSid}:{status}"`).
- **Rank off the ENTRY (caller) channel**, not forwarded/agent/operator legs — the consumer tracks the entry
  `channel.id` per `Linkedid` so an agent leg hanging up first can't mark the call terminal early.
- `initiated` (rank 1) is reserved for outbound `originate` (§6); inbound starts at `ringing`.
- Voicemail / caller-abandoned-in-IVR fold into `completed`/`no-answer` for now (may graduate later).

### 4. Recordings
- **Reuse the `recordings` table + transcription pipeline + retention sweep unchanged** — all format- and
  provider-agnostic (retention keys off `transcribed`/`downloaded_at`/`storage_path`).
- `provider_recording_sid` = the **ARI recording name** we set at record time (e.g. `{Linkedid}` /
  `{Linkedid}-{n}`) — naturally unique/stable.
- The "fetch" step becomes a **local move** (not HTTP download): a new `asterisk` branch that `os.replace`s the
  **WAV** from the Asterisk spool dir into the recordings dir as `.wav`. Storage location is flexible (user:
  "can land anywhere on server/DB"). Existing Twilio/SignalWire `.mp3` behavior is untouched (additive).
- The worker (Docker) must **bind-mount the Asterisk spool dir** to perform the move — deferred to **ticket 09**
  (infra/deploy). `DELETE_REMOTE_RECORDING` semantics for Asterisk = delete the source WAV after a successful
  move so Asterisk's disk doesn't fill.
- **UI (user requirement):** keep **one `calls` table**, segregate purely by `provider_id` — Twilio/SignalWire
  logs in their tab(s), BulkVS/Asterisk in a separate tab. No separate table. Tab layout → **ticket 10**.

### 5. Reconciliation
- BulkVS has no CDR API and Asterisk has no REST "list calls", so the live ARI stream has **no Twilio-style
  backfill** — a worker outage would otherwise lose those calls entirely.
- **Enable Asterisk CDR written directly into our Postgres** (dedicated `asterisk_cdr` table via
  `cdr_adaptive_odbc`/`cdr_pgsql`). Add an **`asterisk` branch to the existing reconciler** that reads it
  windowed (last N hours) and feeds the **same `ingest_status_event`**. CDR `linkedid`/`disposition`/`start`/
  `answer`/`end`/`src`/`dst` map cleanly onto the normalized event.
- CDR is written by Asterisk **independent of the worker**, so it survives worker restarts — the exact gap it
  covers. It heals *completed* calls (one row per call at hangup), not in-progress — which is what
  reconciliation is for. Recordings are discovered separately (ARI record events + spool-dir sweep on reconcile).

### 6. Outbound + AI-agent calls
- **Same `calls` / `call_events`** — an outbound call is one row with `direction="outbound"`,
  `provider_call_sid = Linkedid` (from ARI `originate`), first event `initiated` (rank 1). No new table.
- **Attribution flips by direction:** stamp `campaign_id` from the leg that is *our* DID — `to_number` for
  inbound (as today), **`from_number` for outbound**. Outbound activity thus shows in the same per-campaign
  analytics.
- The reconciler's `_is_inbound` outbound-drop stays a **Twilio-only** hack; the Asterisk CDR branch ingests
  **both directions**. No regression to Twilio.
- **AI-agent calls are not a distinct shape:** agent-answered = `inbound`, agent-placed = `outbound`; what the
  agent did (turns/transcript/outcome) records through the existing `transcriptions` + `call_analysis` tables
  against the same `call_id`. Agent runtime → **ticket 11**.
- **Forwarded / redirected legs** are additional channels under the same `Linkedid` → collapse into the one
  `calls` row via `forwarded_to` (matches today's semantics).

### Newly surfaced (graduated to a ticket)
User confirmed a requirement to **handle calls in-platform** (place/receive directly in the web UI). This means
a **WebRTC softphone leg registered into Asterisk** — data-model-neutral (just another channel under the
`Linkedid`, one `calls` row) but new transport/infra/UX. Ticketed as
[In-platform calling (WebRTC softphone leg)](13-in-platform-webrtc-calling.md), blocked by 06 + 09.

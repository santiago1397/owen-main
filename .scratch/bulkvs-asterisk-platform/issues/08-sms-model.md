# SMS model — send / receive / threads / compliance

Type: grilling
Status: resolved (2026-07-22 — grilling; additive, no regression to Twilio/SignalWire/GHL paths)
Assignee: svillahermosa
Blocked by: 01

## Question

Define the SMS (and MMS?) subsystem on BulkVS messaging.

- **Inbound:** BulkVS delivers inbound messages how (webhook)? New public signed endpoint under `/webhooks/*`,
  event-sourced like calls? New `messages` + `message_threads` tables?
- **Outbound:** send API integration, delivery-receipt handling, retries via the existing Postgres job queue.
- **Threading/UX:** conversation view keyed by (our number, contact) — reuse the `callers` identity? Tie SMS to
  the same campaign/attribution + GHL relay as calls?
- **Compliance:** A2P 10DLC brand/campaign registration — is it a prerequisite blocker for sending, and whose task?
- **MMS:** in for v1 or fog?

Use `/grilling` + `/domain-modeling`.

## Answer

**Grilling, 2026-07-22.** Design locked. **Key reframing: this is NOT greenfield** — a full
**inbound** SMS/MMS subsystem already ships live for Twilio/SignalWire and is the base to extend:
`messages` table (`backend/app/models/models.py:190`), `ingest_message_event`
(`backend/app/services/messages.py`), webhook `POST /webhooks/{provider}/message`
(`backend/app/webhooks/common.py:177`), `parse_message_event` (`twilio.py:48`),
`message_relay_ghl` job (`handlers.py:235`), migration `b4e1a7c92f10_messages.py`.

Two of the ticket's original sub-questions are **already answered by shipped code**:
- **Not event-sourced like calls.** Messages are atomic single-row **upserts** keyed on
  `provider_message_sid` (no `message_events`→projection). A text has no lifecycle to project.
- **Identity + attribution already reuse** `callers` (`_get_or_create_caller`) and number→campaign,
  and already relay inbound to GHL.

### Scope (decided)
Inbound-first, with **manual two-way** for 10DLC-registered numbers. **Automated/flow-triggered
sends are deferred to the flow-graph (ticket 06)** — explicitly not this ticket. Use-case =
service-appointment / customer-care messaging (matches ticket 12's Customer Care / Account
Notification registration). **Additive** — Twilio/SignalWire/GHL paths untouched.

### Net-new work (the actual ticket)

1. **BulkVS messaging adapter** (`providers/bulkvs.py` + `webhooks/bulkvs.py`)
   - **Inbound parse** of `{To:[...], From, Message}`.
   - **Verification = IP allow-list** (`52.206.134.245`, `192.9.236.42`) — BulkVS has no HMAC.
     Requires an **additive extension to `_verified()`** (`webhooks/common.py:79`) to pass the
     request's client IP (honoring `X-Forwarded-For` behind Traefik) so a provider's
     `verify_signature` can IP-check instead of HMAC. **⚠ FLAGGED shared-code change** per the
     map's hard constraint — Twilio/SignalWire keep their HMAC path unchanged.
   - **Synthetic `provider_message_sid`** = `sha256(from|to|body|coarse-timestamp-bucket)` for
     best-effort inbound dedup (the NOT-NULL unique idempotency key). **Known unknown:** confirm
     the real BulkVS inbound payload during implementation (it may carry a ref/timestamp to prefer);
     no separate ticket — verify like ticket 04 proved real calls.
   - **Tracking number** via the existing **per-DID `?tracking_number=+1…`** query override on each
     DID's inbound webhook URL — sidesteps the array `To` ambiguity (same trick SignalWire uses).

2. **Outbound send path** — reuse `messages` with `direction='outbound'`.
   - Operator hits send → row written `queued` → new **`message_send`** job type → **worker** calls
     BulkVS `messageSend` (Basic auth, 11-digit From/To, `To` array,
     `delivery_status_webhook_url`). Durable/retryable via the Postgres queue; UI sees `queued` instantly.
   - `provider_message_sid` from the **`messageSend` API response**.
   - **Forward-only status guard** (queued→sent→delivered/failed; never regress; failed terminal).
     BulkVS receipts are unreliable (ticket 01) → a message may legitimately **rest at `sent`** forever.

3. **Delivery receipts** — dedicated **`POST /webhooks/bulkvs/message-status`** (IP allow-listed),
   forward-only upsert of the outbound row by `provider_message_sid`. Kept separate from inbound
   `/message` (mirrors how calls split `/status` from `/recording`).

4. **Per-number 10DLC gate** — add `numbers.sms_campaign_id` (TCR Campaign ID) + derived
   `numbers.sms_enabled`. **Manual entry** in OWEN for now (the `/tnRecord` DID→campaign field is
   GATED per ticket 12; auto read-back = fog). `message_send` **refuses** when unset; UI shows a
   read-only reply box + "not 10DLC-registered" badge. This is the concrete bridge from **ticket 12**.

5. **Threading** — **derived by query** on `(number_id, caller_id)` (the resolved phone identity;
   GHL contact ids stay out of the key). Both directions interleaved by time. Small **`last_read_at`
   per (number_id, caller_id)** for unread badges. First-class `message_threads`
   (assignment/archive/status) = fog until inbox-workflow features are needed.

6. **Opt-out compliance** — app-level **`sms_opt_outs`** keyed **per (number, contact)** (matches the
   threading key + carrier STOP semantics). `message_send` **hard-refuses** opted-out contacts
   regardless of what BulkVS does upstream. Inbound **STOP** sets, **START** clears, **HELP** fires a
   templated auto-reply. (Compliance is a top A2P rejection cause — ticket 12.)

7. **GHL** — inbound relay already live. **Outbound also relays** on reaching `sent`
   (`post_outbound_message` alongside `post_inbound_message`, same once-guard) so GHL's thread shows
   both sides ("see them in GHL"). **Respond-*from*-GHL** (GHL→OWEN→BulkVS) = **fog** — a separate
   authenticated inbound integration; in-system reply is the priority.

8. **UI/API** — new JWT `/api/*` surface: list threads, get a thread, send, mark-read. **Polling**
   (React Query, tighter interval on the focused thread) — honors ARCH decision 13, **no WebSockets**.
   Reply always goes out on the **thread's number** (no picker). Nullable **`sent_by_user_id`** on
   outbound rows for audit (automated/flow sends leave it null).

9. **MMS** — inbound stored + rendered as **provider media URLs as-is** (`num_media`/`media_urls`
   already modeled). **Outbound MMS = fog** (v1 outbound is SMS-only; outbound MMS needs an
   HTTPS media-hosting story). Local persistence of inbound MMS media (mirroring the recording
   fetcher) = fog, only if expiring URLs bite.

### Data-model deltas (all additive)
- `numbers`: `+ sms_campaign_id` (nullable, TCR Campaign ID), `+ sms_enabled` (derived).
- `messages`: `+ sent_by_user_id` (nullable FK users); outbound rows use `direction='outbound'`.
- New `sms_opt_outs` (`number_id`, `caller_id`, `opted_out_at`) — or `opted_out_at` on the read-state row.
- New `sms_read_state` (`number_id`, `caller_id`, `last_read_at`).
- New job type `message_send`; new endpoint `POST /webhooks/bulkvs/message-status`.

### Dependencies
- **Outbound *enablement*** per-number waits on **ticket 12** (10DLC brand+campaign approval).
- **Automated/flow SMS sends** wait on **ticket 06** (flow-graph) — out of this ticket by design.

### Fog surfaced (added to map "Not yet specified")
Respond-from-GHL; automated/flow SMS sends (→06); outbound MMS + media hosting; local MMS-media
persistence; auto read-back of `sms_campaign_id` from `/tnRecord` (needs ticket 12 to confirm the
field); first-class `message_threads` (inbox workflow); real-time SSE for live-chat feel.

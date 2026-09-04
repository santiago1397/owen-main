<!-- wayfinder:spec -->
# Spec: BulkVS + Asterisk number-management platform

> Synthesized from the completed wayfinder map (`map.md`) and its 15 resolved tickets. This document
> hands the locked decisions to implementation. It plans behavior and contracts, not file paths.
> **Hard constraint on every line below: this is additive.** The live Twilio, SignalWire, and GHL
> integrations must keep working unchanged; Asterisk/BulkVS is a *third provider alongside* them, never a
> replacement. All new surface is flag-gated (`ASTERISK_ENABLED`, default off) and reversible.

## Problem Statement

OWEN today is a passive call-**attribution** tool: it ingests call/SMS events from Twilio and SignalWire,
records and transcribes calls, runs LLM analysis, and relays leads to GHL. The operator cannot *act* on their
phone numbers from inside OWEN. To change how a number behaves — where it forwards, whether it records, what
callers hear, business-hours handling, an IVR menu — they must leave OWEN and configure a separate carrier
console. They cannot send or reply to SMS from OWEN. They cannot place a call from OWEN. They have no way to
put an AI voice agent on a number. The result is a split-brain workflow: OWEN *sees* the telephony but does not
*run* it, so every operational change means context-switching to another vendor's UI and hoping it stays in
sync with what OWEN reports.

## Solution

Turn OWEN into an active **number-management platform** on top of **BulkVS** (DIDs + SIP trunk) and
**Asterisk** (the media brain, driven from the backend over ARI), while keeping every existing integration
intact. From one operator UI the single-org operator can:

- **See and label every number** they own (BulkVS DIDs mirrored in, alongside read-only Twilio/SignalWire
  numbers), with derived lifecycle state (available / assigned / released).
- **Define per-number call behavior** — forward, record, voicemail, business hours, IVR menu — through a
  simple linear rule form, persisted as a **flow-graph** so a visual builder can layer on later without a
  rewrite. The flow executes live on Asterisk via an ARI interpreter.
- **Send and receive SMS** on 10DLC-enabled BulkVS numbers, with a two-pane inbox, manual two-way threads,
  opt-out handling, and GHL relay — reusing the existing inbound SMS subsystem.
- **Run AI voice agents** as flow nodes that answer calls, converse (OpenAI Realtime over on-box audio),
  capture leads, send SMS, and transfer — feeding the same recording/transcription/analysis/GHL pipeline.
- **Place and receive calls in the browser** via a WebRTC softphone leg, including manual outbound calls from
  an owned BulkVS caller ID with a recording-consent notice.

Asterisk becomes a **third provider** feeding the same event-sourced `call_events`→`calls` projection, so all
existing attribution, recording, transcription, analysis, and GHL-relay logic is reused, not reinvented.

## User Stories

### Numbers — inventory, labels, lifecycle
1. As an operator, I want my BulkVS DIDs to appear automatically in OWEN, so that I don't hand-enter numbers.
2. As an operator, I want a background sync to pull new BulkVS numbers on a schedule, so that inventory stays current without a webhook (BulkVS has none — it polls `/tnRecord`).
3. As an operator, I want a number I release in the BulkVS portal to be soft-released in OWEN (marked inactive, history frozen) rather than deleted, so that past call/attribution history survives.
4. As an operator, I want a DID I re-buy to reactivate its original OWEN row, so that its history is continuous.
5. As an operator, I want to edit a number's friendly label in OWEN, so that I can recognize it — mirrored one-way from BulkVS `ReferenceID` for v1.
6. As an operator, I want each number to show its owner provider (BulkVS) and media provider (Asterisk) distinctly, so that I understand which system routes it.
7. As an operator, I want a number's lifecycle state (available / assigned / released) derived from its data, so that I never manage a status field by hand.
8. As an operator, I want my existing Twilio/SignalWire numbers to still appear (read-only, no flow authoring), so that I keep one inventory view across all providers.
9. As an operator, I want the Numbers page to be the operator hub — a table with owner→media, flow, campaign, and SMS-state columns, drilling into a per-number detail — so that I manage everything from one surface.

### Call flows — authoring and execution
10. As an operator, I want to define what happens when a number is called, so that inbound calls are handled without a carrier console.
11. As an operator, I want a simple linear rule form (hours → greeting + record → IVR menu → default routing → fallback), so that I can author a flow without learning a graph editor.
12. As an operator, I want to forward a call to another number, so that calls reach the right person.
13. As an operator, I want to record a call (as a modifier on the flow), so that recordings feed the existing pipeline.
14. As an operator, I want business-hours branching, so that after-hours calls are handled differently.
15. As an operator, I want an IVR menu ("press 1 for sales"), so that callers self-route.
16. As an operator, I want voicemail capture when no one answers, so that no caller hits dead air.
17. As an operator, I want a mandatory greeting/consent notice option, so that I meet FL all-party recording-consent rules.
18. As an operator, I want one flow to be reusable across many numbers, so that I don't duplicate configuration.
19. As an operator, I want my edits saved as a new immutable flow version, so that in-flight calls keep running the version they started on.
20. As an operator, I want the system to validate a flow before I can activate it (one entry, resolvable targets, type-correct ports = hard errors; unreachable/unwired/cycles = warnings), so that I don't publish a broken flow.
21. As an operator, I want any unwired or errored path to fall through to a flow-level fallback (usually voicemail), so that calls never dead-air.
22. As a system, I want each flow node transition to emit exactly one `call_event`, so that the existing projection captures the whole call.
23. As an operator, I want a call to pin its flow version at the moment it starts, so that reporting reflects what actually ran.

### Asterisk as a provider — data model & ingestion
24. As a system, I want Asterisk calls to land in the same `call_events`→`calls` projection as Twilio, so that all downstream logic is reused.
25. As a system, I want one `calls` row per Asterisk call keyed on `Linkedid`, so that multi-leg calls collapse to a single record.
26. As a system, I want Asterisk channel lifecycle mapped into the existing Twilio-CallStatus vocabulary, so that the UI and analytics need no special cases.
27. As a system, I want a persistent ARI-WebSocket consumer inside the single-replica worker to ingest events (no webhook; signature verification is a no-op for this provider), so that ingestion matches the existing job-queue architecture.
28. As a system, I want duplicate status events deduplicated per `"{Linkedid}:{status}"`, so that retries and multi-leg noise don't double-count.
29. As a system, I want Asterisk CDR reconciled into Postgres, so that calls are not lost if the worker restarts mid-call.
30. As a system, I want recordings from Asterisk to reuse the existing recordings table and fetch/transcribe pipeline, so that no parallel recording system exists.
31. As an operator, I want the call log to show Asterisk/BulkVS calls in a Platform sub-tab and Twilio/SignalWire in an Attribution sub-tab of the same Calls page, so that I see everything in one place segregated by provider.
32. As a system, I want outbound and AI-agent calls to be the same `calls` rows distinguished by `direction`, with outbound campaign attribution resolved via `from_number`, so that outbound needs no new schema.

### SMS
33. As an operator, I want to receive SMS/MMS on my BulkVS 10DLC numbers, so that texts reach me in OWEN.
34. As an operator, I want a two-pane Messages inbox with threads grouped by (number, caller), so that conversations are easy to follow.
35. As an operator, I want to reply manually to a thread from that thread's number, so that I can hold two-way conversations.
36. As an operator, I want the composer disabled on numbers that aren't 10DLC-enabled, so that I don't send messages that will be blocked.
37. As an operator, I want STOP/START/HELP handled at the app level with per-(number, contact) opt-out, so that I stay compliant.
38. As an operator, I want inbound and outbound SMS relayed to GHL, so that leads flow to the CRM as they do today.
39. As a system, I want the BulkVS SMS adapter to verify inbound webhooks by IP allow-list (additive extension to the existing verifier), so that spoofed messages are rejected.
40. As a system, I want outbound SMS to run as a worker job to BulkVS `messageSend` with forward-only status, reusing the messages table (`direction='outbound'`), so that sending matches the existing job pattern.
41. As an operator, I want per-number SMS gating (`sms_enabled` + `sms_campaign_id`), so that only registered numbers can send.
42. As an operator, I want the inbox to poll for updates (no websocket), so that it stays simple and consistent with the rest of the app.
43. As an operator, I want to see who on my team sent each outbound message (`sent_by_user_id`), so that there's an audit trail.

### AI voice agents
44. As an operator, I want to create reusable AI voice agents as first-class versioned objects, so that I can put the same agent on multiple flows and version its config.
45. As an operator, I want to configure an agent's persona, voice, greeting, model, tools, in-context knowledge, and guardrails, so that it behaves the way I want.
46. As an operator, I want to drop an AI agent into a flow as a node (never bound directly to a number), so that agents compose with the rest of the flow.
47. As a system, I want the agent version pinned when a call enters the node, so that mid-call config changes don't affect running calls.
48. As a caller, I want the agent to respond naturally with barge-in (server-VAD), so that I can interrupt like a real conversation.
49. As an operator, I want the agent to capture leads and send SMS in-call via a fixed tool registry (per-agent toggles, no arbitrary LLM HTTP), so that behavior is safe and bounded.
50. As an operator, I want the agent to transfer or end the call by returning through the flow node's ports (agent never bridges directly), so that the flow interpreter stays in control.
51. As a system, I want the agent transcript written inline to the transcriptions table (speaker-labeled, skipping post-call STT for agent legs), so that transcripts reuse the existing store.
52. As a system, I want per-agent guardrails (`max_call_seconds`, `max_silence_seconds`, model tier), so that runaway calls are bounded.
53. As a system, I want any agent failure to route to the `failed` port → fallback (voicemail) after one reconnect retry, so that callers never hit dead air.
54. As a system, I want the agent runtime behind a `VoiceAgentSession` seam mirroring `TranscriptionEngine`, with a global kill-switch and a `dummy` engine, so that it's pluggable and testable.

### In-platform calling — WebRTC softphone
55. As an operator, I want to answer platform calls in my browser, so that I don't need a desk phone.
56. As an operator, I want to place outbound calls from an owned BulkVS caller ID, so that my outreach shows the right number.
57. As an operator, I want to pick a from-number and have my default remembered, so that dialing is fast.
58. As an operator, I want a "call" action on caller, contact, and missed-call records, so that I can dial in one click from context.
59. As a callee, I want a recording-consent notice played before the call bridges (on by default for outbound), so that recording is compliant.
60. As an operator, I want soft, non-blocking guardrails (warn on opt-out hit or outside 8am–9pm callee-TZ) that I can override, so that I'm nudged but not blocked.
61. As an operator, I want to transfer a live call to a DID, another operator, or an AI-agent runtime (blind transfer for v1), so that I can hand calls off.
62. As an operator, I want my availability derived from browser registration AND an app toggle, so that calls only ring me when I'm actually available.
63. As an operator, I want an in-call bar with hold/transfer/hangup, driven by the backend over ARI (never browser→ARI directly), so that call control is secure and centralized.
64. As a system, I want a missed platform call (no flow, no answer) captured and answerable in-app, so that no inbound call is lost.

### Cross-cutting operator UX
65. As an operator, I want the sidebar grouped into Attribution (existing), Platform (Numbers, Call Flows, Messages, AI Agents), and System, so that the new surface is discoverable without disturbing existing pages.
66. As an operator, I want all new UI to be additive to the existing React SPA, so that nothing I rely on today changes.
67. As an operator, I want a Call Flows library (one flow → many numbers) and an AI Agents library, so that I manage reusable assets in one place.

### Deploy, security, health (operator/operator-owner facing)
68. As an operator-owner, I want the whole platform flag-gated (default off) and reversible, so that I can dark-deploy, flip on, and roll back by flipping the flag.
69. As an operator-owner, I want Asterisk config living in-repo and deployed by rsync + targeted reload (never a restart), so that deploys don't drop live calls.
70. As an operator-owner, I want SIP locked to the 4 BulkVS SBC IPs and ARI reachable only from the pinned Docker subnet, so that the trunk and control plane aren't exposed.
71. As an operator-owner, I want a non-gating `/health/telephony` endpoint plus scheduler warnings on trunk-down / ARI-disconnect / RTP exhaustion, so that I can see telephony health without the deploy healthcheck depending on it.
72. As an operator-owner, I want Asterisk run as a systemd service (`Restart=always`, version pinned) decoupled from `make deploy`, so that its lifecycle is stable and independent of app deploys.

## Implementation Decisions

### Providers, media path, tenancy
- **Media brain = Asterisk**, native on the existing host (already installed, verified 22.10.1), driven from the backend over **ARI**. BulkVS SIP trunk → Asterisk. No CPaaS in the voice media path.
- **Single-org** internal tool. No per-tenant isolation, no billing.
- BulkVS trunk facts: UDP/5060, IP-auth `chan_pjsip` endpoint + aor + identify, `direct_media=no`, ulaw/RFC2833, **no TLS/SRTP**. **RURI is `+E.164`** (not 11-digit). Signaling from 4 SBC IPs (162.249.171.198 / 76.8.29.198 / 69.12.88.198 / 199.255.157.198); **RTP media arrives from a different range (`152.188.166.x`)** so RTP ports cannot be IP-locked to the SBC IPs.
- BulkVS API: REST `portal.bulkvs.com/api/v1.0` (Basic auth). `/tnRecord` (list/route/label), `/orderTn`+`/exchanges` (search/buy — out of scope), `/trunkGroups`. **No inventory webhook → poll.** **No CDR API → source calls from Asterisk.**

### Event-sourced data model (Asterisk as 3rd provider)
- Asterisk is a provider `name="asterisk"` on the **same `call_events`→`calls` projection**. `provider_call_sid = Linkedid`; **one `calls` row per call** (legs collapse).
- Ingestion = a **persistent ARI-WebSocket consumer inside the single-replica worker** (no webhook route; `verify_signature` is a no-op for this provider) feeding the existing `ingest_status_event`.
- An `_ARI_TO_STATUS` map projects channel lifecycle into the existing Twilio-CallStatus vocabulary, **ranked off the entry channel**; dedup key `"{Linkedid}:{status}"`.
- **Recordings reuse** the existing table + fetch/transcribe pipeline (local WAV move; Asterisk spool bind-mount handled in infra).
- **Reconciliation via Asterisk CDR → Postgres** into the same projection (survives worker restart).
- Outbound/agent calls are the same rows; `direction` distinguishes them; outbound attributes `campaign_id` via `from_number`. The `_is_inbound` drop rule stays **Twilio-only**.

### Numbers — lifecycle & assignment
- **Split identity:** `numbers` gains `owner_provider` (bulkvs) vs `media_provider` (asterisk). `calls.provider_id == media_provider`; attribution resolves by `(media_provider, to_number)`. Owner is number-only.
- **Sync:** buy/release happens in the BulkVS portal (in-app buying out of scope). A `sync-numbers` BulkVS adapter **polls `/tnRecord`**, add-only; label **one-way mirrored** from `ReferenceID` → `friendly_name`. A vanished DID **soft-releases** (`active=false` + `released_at`, history frozen); a re-bought DID **reactivates the same row**.
- **Routing:** every DID routes to Asterisk once at provision; the Stasis app branches on dialed `+E.164`. No per-flow BulkVS change.
- **Assignment:** a number points to one **shared** `flow_id` (flow-graph) plus the existing `campaign_id`. **AI agents attach only as flow nodes, never to a number.** No-flow ladder: flow → legacy `forwards_to` → capture as a **missed call**, answerable in-app.
- **Lifecycle is derived** (available / assigned / released) from `active` + `released_at` + `flow_id`/`campaign_id`. No status enum.
- BulkVS `ReferenceID` is the `friendly_name` analogue, readable and writable via the API; v1 mirrors one-way (two-way write-back deferred). `Lidb` = CNAM, not a private label.

### Call-flow graph schema & interpreter
- A **true directed graph** stored as `graph jsonb` inside an append-only **`flows` / `flow_versions`** envelope. A call **pins `flow_version_id` at `StasisStart`** (like `campaign_id` at ingest).
- Nodes = object map keyed by id. Edges = each node's **`next` map keyed by port**. Unwired/errored ports fall to a flow-level **`default_fallback`** (usually voicemail).
- Node set: `entry`, `play`, `hours`, `menu`, `dial`, `voicemail`, `ai_agent`, `hangup`. **`record` is a modifier, not a node.** `play` carries the FL consent notice. `dial` supports an **operator-target kind** (individual + group) for softphone routing.
- **In-memory ARI interpreter** emits **one `call_event` per node transition**. No persisted cursor (a worker restart drops RTP anyway).
- **Validation blocks activation:** one entry / resolvable targets / type-correct ports = hard errors; unreachable / unwired / cycle = warnings.
- The graph is the model; **v1 authoring is one simplified linear-form emitter** (`origin`-tagged for round-trip). End-state authoring is a Twilio-Studio-style visual builder (deferred).

### SMS/MMS subsystem
- **Not greenfield:** a full **inbound** SMS/MMS subsystem already ships (Twilio/SignalWire) — `messages` table, `/webhooks/{provider}/message`, `parse_message_event`, `message_relay_ghl`. Messages are **atomic upserts on the SID** (not event-sourced).
- **Scope:** inbound-first + **manual two-way** for 10DLC numbers. Automated/flow-triggered sends belong to the flow-graph (out of scope here).
- **Net-new:** a BulkVS adapter (IP allow-list via an **additive `_verified()` extension** — a flagged shared-code change; synthetic `sha256(from|to|body|ts)` SID, real payload confirmed in impl; per-DID `?tracking_number=`); outbound reuses `messages` (`direction='outbound'`) via a `message_send` worker job → BulkVS `messageSend` (forward-only status, may rest at `sent`); a dedicated `POST /webhooks/bulkvs/message-status`.
- **Per-number gate:** `numbers.sms_campaign_id` + `sms_enabled` (manual entry; bridged from 10DLC registration).
- Threads **derived by `(number_id, caller_id)`** + `last_read_at`. **App-level opt-out** `sms_opt_outs` per (number, contact) with STOP/START/HELP. Outbound also relays to GHL. **Polling** UI (no WS). `sent_by_user_id` audit. Inbound MMS URLs used as-is; outbound MMS deferred.

### AI voice agents
- Agent = **first-class versioned `agents` / `agent_versions`** object referenced by the `ai_agent` node (`agent_version_id` pinned on node entry). Config = persona / voice / greeting / model / `tools[]` / in-context `knowledge` / guardrails.
- **Runtime:** an on-box `VoiceAgentSession` asyncio task in the worker, **AudioSocket/TCP ↔ OpenAI Realtime ↔ call bridge**. **OpenAI server-VAD** barge-in with eager outbound-buffer flush.
- **Tools** = fixed registry, per-agent toggles: flow-exit `transfer` / `end_call` (→ node ports), in-call `capture_lead` / `send_sms`. No arbitrary LLM HTTP.
- **Node exit:** session returns `{port, data}`; the interpreter drives the graph edge. **The agent never bridges** (transfer port → wired `dial` / `ai_agent`).
- **Seam** mirrors `TranscriptionEngine` (`VoiceAgentSession` Protocol + registry + per-agent `engine`; global `VOICE_AGENT_ENGINE` kill-switch). **v1 = `dummy` + `openai_realtime`**; `diy`/`vapi` stubbed.
- **Data:** transcript written **inline** to `transcriptions` (speaker-labeled; agent legs skip post-call STT). Bridge WAV via the `record` modifier. Node-level `call_events`. Existing analysis/GHL/attribution reused, with `capture_lead` → `call_analysis.captured` authoritative.
- **Guardrails:** per-agent `max_call_seconds` / `max_silence_seconds` / `model` tier (no mid-call cost meter). **Failure:** all errors → `failed` port → `default_fallback` (voicemail), with 1 WS-reconnect retry.

### In-platform WebRTC softphone + manual outbound
- Operator softphone = an **additive WebRTC seam**: the browser is a per-operator **`chan_pjsip` WebRTC** endpoint (SIP.js, `wss` + DTLS-SRTP), one channel under `Linkedid`. **Separate seam** from the AI external-media path.
- **Auth:** a static per-operator PJSIP endpoint in `asterisk/pjsip.conf`; the **session SIP password is minted by the backend at app-login** (real gate = app login).
- **Transport:** signaling `wss` **fronted by Traefik** (no new cert lifecycle); media DTLS-SRTP **direct to the existing `10000–10200/udp` RTP range**; `icesupport` advertises the VPS public IP; **coturn** added (TLS relay over 443) for firewall traversal, TURN creds backend-minted.
- **Control split:** SIP.js drives only its own leg (INVITE/answer/BYE); **all bridge/hold/transfer = backend ARI** via FastAPI, never browser→ARI. **Transfer = imperative ARI ops** (DID / operator / AI-agent runtime), **blind for v1** (attended = deferred).
- **Presence/routing:** reuse `dial` with the operator-target kind; availability = browser-registered AND app-toggle; no-answer → `default_fallback`.
- **Manual outbound (thin delta on the softphone):** operator dials → SIP.js INVITEs Stasis → backend ARI **originates the BulkVS trunk leg + bridges** → one `calls` row (`direction='outbound'`, `provider_id=asterisk`, `campaign_id` via from-number). **No new schema.** Net-new: a **from-number picker + remembered default** (CLI must be an **owned** BulkVS DID); **recording on by default with a pre-bridge ARI `play` consent notice** to the callee; **soft non-blocking guardrails** (warn on `sms_opt_outs` hit / outside 8am–9pm callee-TZ, operator may proceed). Entry = the dialer + a "call" action on caller/contact/missed-call records.

### Operator UX surface
- **All additive** to the existing React SPA (clickable mockup exists at `prototypes/10-operator-ux.html`).
- **IA:** the sidebar splits into grouped sections — *Attribution* (existing Dashboard/Calls/Callers/Email Log, untouched), *Platform* (**Numbers · Call Flows · Messages · AI Agents**, new), *System* (Settings).
- **Numbers = the operator hub:** the read-only table becomes the primary surface (owner→media / flow / campaign / SMS-state columns); a row opens number detail (label, derived lifecycle badge, SMS gate, fallback-forward, flow authoring). Twilio/SignalWire rows are read-only.
- **Call log:** provider split = **two sub-tabs on the existing Calls page** (Attribution = Twilio/SignalWire | Platform = BulkVS/Asterisk), same `calls` table by `provider_id`, existing `CallDrawer` unchanged; the Platform tab hosts in-call actions.
- **Rule form:** v1 authoring = a **linear 5-section form** (hours → greeting + record-modifier → IVR menu → default routing → fallback) that emits the simplified graph; a new append-only version on save; Validate runs the schema checks. Visual builder = a disabled "later" tab.
- **SMS:** a two-pane **Messages inbox**, threads by `(number, caller)`, polling, reply on the thread's number, composer disabled when not-10DLC.
- **AI Agents:** a dedicated **library**; an agent is picked from a dropdown inside a flow node — never bound to a number.
- **In-call bar** fills the reserved live-call slot (softphone).

### Infra, security, deploy
- Native Asterisk reuses the **Postgres-on-host + per-project-bridge-subnet-allowlist** mold. Every new surface is **additive, flag-gated (`ASTERISK_ENABLED`, default off), reversible**.
- Config we own → **in-repo `asterisk/` dir**; deploy = rsync + **targeted reload (never restart)**; secrets from `.env.prod`.
- Firewall **asymmetric:** SIP `5060/udp` IP-locked to the 4 SBC IPs (no open registration + fail2ban); RTP `10000–10200/udp` open-but-session-validated (can't IP-lock — media from `152.188.166.x`).
- **ARI `8088`** bound loopback + host-gateway, reached via `host.docker.internal`, UFW-allowed **only from the pinned `callmon-net` subnet** (same fix applied to Postgres `pg_hba`); creds → `.env.prod`.
- Health: a separate **non-gating `/health/telephony`** + APScheduler warnings on trunk-down / ARI-WS-disconnect / RTP exhaustion; the deploy healthcheck stays app-only.
- Daemon lifecycle = **systemd (`Restart=always`) + `apt-mark hold` 22.10.1, decoupled from `make deploy`**; planned restarts drain via `core restart when convenient`.
- Coexistence **by construction:** the only shared surfaces are the additive `call_events` rows and a *separate* flag-gated worker module.

## Testing Decisions

**What makes a good test here:** exercise external behavior at the seam, never implementation details. A flow test
should assert the sequence of `call_events` and the ARI operations requested for a given flow-version + simulated
channel lifecycle — not the interpreter's internal structures. Provider tests should assert the resulting rows in
`call_events` / `calls` / `messages` for a given raw provider payload — not adapter internals. This keeps tests
stable as internals change and mirrors how the existing Twilio/SignalWire ingestion is already tested.

**Seams to test (prefer existing, highest possible — four total, three already exist):**

1. **Event-sourced ingestion seam (existing, reused).** `ingest_status_event` / `parse_message_event` and the
   `call_events`→`calls` / `messages` projections. Asterisk enters here as a third provider exactly like Twilio.
   *Test:* feed raw ARI channel events (and BulkVS SMS payloads) → assert projected rows, status mapping
   (`_ARI_TO_STATUS`), `Linkedid`-keyed single-row collapse, and `"{Linkedid}:{status}"` dedup. **Prior art:** the
   existing Twilio/SignalWire ingestion tests around `ingest_status_event` / `parse_message_event`.

2. **ARI-interpreter seam (new — the one genuinely new seam, placed at the highest point).** The flow interpreter
   run against a **faked ARI client**. *Test:* given a flow-version graph + a simulated channel + DTMF/answer/hangup
   events, assert the emitted `call_events` (one per transition), the ARI ops requested (play/record/dial/originate/
   bridge), fallback-to-`default_fallback` on unwired/errored ports, and version-pinning at `StasisStart`. Also test
   **flow validation** (hard errors block activation; warnings don't) as a pure function over a graph. **Prior art:**
   none directly; model the fake-client style on how existing provider adapters are unit-tested.

3. **Provider-adapter seams (existing pattern, reused).** BulkVS number-sync against a faked `/tnRecord` response
   (add-only, soft-release on vanish, reactivate on return, one-way label mirror); BulkVS SMS send job against a
   faked `messageSend` and the `/webhooks/bulkvs/message-status` handler; the additive `_verified()` IP allow-list.
   **Prior art:** existing sync/adapter and webhook-handler tests.

4. **`VoiceAgentSession` / engine-registry seam (existing `TranscriptionEngine` pattern, reused).** Test the agent
   node with the **`dummy` engine**: node entry pins `agent_version_id`, tool calls (`capture_lead`/`send_sms`/
   `transfer`/`end_call`) map to the right node ports and side effects, guardrail limits terminate the session, and
   failures route to the `failed` port → fallback after one retry. **Prior art:** the existing `TranscriptionEngine`
   Protocol/registry tests.

**Modules tested:** the Asterisk ingestion consumer + status mapping; the flow interpreter + validator; the BulkVS
number-sync adapter; the BulkVS SMS send/receive/status adapter + opt-out; the AI-agent runtime seam (dummy engine);
the softphone/outbound origination control path (backend ARI ops, faked client). Live end-to-end trunk behavior was
already proven on real infra in ticket 04 and is not re-litigated by unit tests.

## Out of Scope

- **Multi-tenant SaaS + usage billing** — single-org tool. Fresh effort only.
- **Full Twilio number porting / cutover / decommission** — coexistence only.
- **In-app number buying/provisioning** (BulkVS `/orderTn` + `/exchanges`) — operator buys/releases in the BulkVS portal; OWEN mirrors inventory + assigns behavior.
- **AI outbound dialing / campaign dialing** — the whole campaign machinery (contact lists, `outbound_calls` queue, pacing, AMD, TCPA/consent for AI-placed voice, cost guardrails). Ticket 15 pivoted to *manual human* outbound; AI outbound returns as a fresh effort.
- **Visual flow-builder canvas** — the graph schema is built for it; v1 ships the linear rule form. Builder UI is a disabled "later" tab.
- **Two-way BulkVS label write-back** — feasibility confirmed, not built for v1 (one-way mirror only).
- **Attended (consult) transfer** for the softphone — v1 is blind transfer; attended is a fast-follow.
- **Outbound MMS** and local persistence of inbound MMS media.
- **Respond-from-GHL** (GHL → OWEN → BulkVS outbound), first-class `message_threads` table, real-time SSE for SMS.
- **Additional flow-node vocabulary** the schema supports but v1 doesn't build: `condition`, blind-`transfer` distinct from `dial`, `queue`/hold, `goto`/subflow.
- **AI-agent deferred enhancements:** custom webhook tools, RAG + `lookup_knowledge`, a separate media process, agent-chosen multi-destination transfer, `diy_pipeline`/`vapi_sip` engines, aggregate cost-budget alarms.
- **Telephony alerting wiring** — ticket 09 lands health *signals*; routing them to a notification channel is deferred.
- **Asterisk HA / failover / concurrency ceilings.**
- **Dashboard platform-awareness** (BulkVS/Asterisk breakdown + SMS tiles on the analytics dashboard).
- **General per-leg recording-consent model** beyond the entry-`play` (inbound) and pre-bridge-`play` (outbound) notices already specified.

## Further Notes

- **Sequencing.** The safe build order implied by the tickets: (1) Asterisk provider + trunk + data-model
  ingestion (ticket 05, on top of the proven-on-real-infra trunk from ticket 04); (2) numbers sync/lifecycle
  (07); (3) flow-graph schema + interpreter (06); (4) operator UX shell + rule form (10); then the independent
  branches (08 SMS, 11 AI agents, 13 softphone + 15 outbound) which each layer on the core. Infra/security (09)
  underpins everything and ships flag-gated first.
- **10DLC is a HITL prerequisite for outbound SMS**, tracked in ticket 12 (A2P 10DLC brand + campaign
  registration, in-progress, awaiting the account holder). Inbound SMS and all voice work do not depend on it;
  outbound SMS enablement bridges from it via `numbers.sms_campaign_id` + `sms_enabled`.
- **Real-infra grounding.** Ticket 04 already proved a real inbound PSTN call answered with audio, an ARI-
  originated outbound call, and an ARI-produced recording on Asterisk 22.10.1 — so the trunk, RTP quirks, and ARI
  control path in this spec are grounded in reality, not assumption.
- **Flagged shared-code changes** (call them out explicitly in implementation, don't do them silently): the
  additive `_verified()` extension to the SMS webhook verifier, and the pinned `callmon-net` subnet fix applied to
  both ARI/UFW and Postgres `pg_hba`.
- Detailed per-decision rationale and any ADRs live in the 15 resolved ticket files under
  `.scratch/bulkvs-asterisk-platform/issues/`; `map.md` is the one-line-per-ticket index.

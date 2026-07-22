# Operator UX/UI surface

Type: prototype
Status: resolved (2026-07-22 — prototype + HITL; IA & rule-form layout locked, all-additive to existing UI)
Assignee: svillahermosa
Blocked by: 06, 07

## Question

What does the operator actually see and click — the "easiest UX, best of QUO/Twilio/SignalWire" ask made concrete?

- Information architecture: where do Numbers, Call flows, SMS conversations, AI agents, and the existing
  attribution/analytics dashboards sit relative to each other in one app?
- **Call-log tab segregation (user requirement, from [ticket 05](05-asterisk-as-provider-data-model.md)):** the
  existing **Twilio + SignalWire** call logs and the new **BulkVS/Asterisk** features live in **separate tabs**.
  Data-model side is settled (one `calls` table, filter by `provider_id`); this ticket decides the actual tab IA.
- The **per-number rule form** (from ticket 06's graph) — a rough clickable/stub layout to react to.
- The **SMS conversation** view (from ticket 08).
- How AI-agent config surfaces (from ticket 11) without cluttering the simple path.
- In-call operator UI (place/receive/transfer) coordinates with [ticket 13](13-in-platform-webrtc-calling.md).

Use `/prototype`; link the artifact from this ticket. Decide the IA + the rule-form layout; defer the visual
flow-builder canvas (fog).

## Answer

**Prototype + HITL, 2026-07-22.** IA and the v1 rule-form layout are locked. Clickable mockup:
[`prototypes/10-operator-ux.html`](../prototypes/10-operator-ux.html) (throwaway; matches the existing dark
`styles.css` house style — grouped sidebar, right-drawer, pill badges, React-Query-over-`api.ts`). Everything
here is **additive to the existing React SPA** — the current Dashboard/Calls/Callers/Email Log/Settings pages
are untouched (map hard constraint).

### Information architecture — grouped sidebar (decided)
The single 210px sidebar stays the one extension point, now split into **three labeled groups**:
- **Attribution** (existing, unchanged): Dashboard · **Calls** · Callers · Email Log.
- **Platform** (all new): **Numbers** · **Call Flows** · **Messages** · **AI Agents**.
- **System**: Settings.

This keeps the shipped attribution app visually intact while signaling the new capability set. (Rejected: a
*flat* list — 9+ items blur old vs new; a *numbers-hub-only* IA — flows/agents are reusable across numbers per
ticket 07, so they need reachable libraries, not only number-drill-in.)

### Numbers = the operator hub (decided)
The existing read-only `Numbers` page becomes the **primary operator surface**. The table gains an at-a-glance
column set: **Owner→Media** provider (`BulkVS→Asterisk` per ticket 07's split identity), assigned **Call flow**,
**Campaign**, **SMS** state (10DLC on / not-registered / receive-only), last call. Row → **number detail**:
label (BulkVS `ReferenceID`, ticket 14), campaign, derived lifecycle badge (ticket 07 — no status column),
SMS/10DLC state, fallback-forward, and the call-flow authoring surface. **Twilio/SignalWire rows are read-only
mirrors** — no flow authoring (Asterisk-only). Buying/releasing DIDs stays in the BulkVS portal (ticket 07 out-of-scope).

### Call-log segregation — sub-tabs within Calls (decided)
The user's ticket-05 requirement (Twilio+SignalWire vs BulkVS/Asterisk in **separate tabs**) is realized as
**two sub-tabs on the one existing Calls page**: **Attribution** (Twilio · SignalWire) | **Platform calls**
(BulkVS / Asterisk). One `calls` table filtered by `provider_id` (settled in 05); the existing hard-coded
provider dropdown is subsumed by the tab. Row click keeps the **existing `CallDrawer`** unchanged (recording /
AI analysis / transcript / event timeline). The **Platform calls** tab is where live-Asterisk in-call actions
attach (→ ticket 13). (Rejected: two separate sidebar items — splits a familiar page and costs a nav slot.)

### Per-number rule form — linear 5-section form (decided)
v1 flow authoring is a **linear rule form**, the ticket-06 *simplified emitter* of the flow-graph (same graph
the builder edits later — no rewrite; `origin`-tagged for round-trip). Sections, top-to-bottom:
1. **Business hours** (toggle + schedule; outside-hours jumps to the after-hours step)
2. **Greeting** (`play`; carries the FL all-party recording-consent notice) + a **Record this call** modifier toggle (ticket 06: `record` is a modifier, not a node)
3. **Menu / IVR** (optional; each key → a step: dial team / **AI agent** / ring operator browser / voicemail)
4. **Default routing** (no-menu / no-key: dial · AI agent · voicemail · forward-to-number)
5. **Fallback** (catch-all → voicemail, so no call hits dead air — ticket 06 `default_fallback`)
Save writes a **new append-only flow version**; **Validate** surfaces ticket-06 activation errors/warnings.
The **visual Twilio-Studio-style builder is deferred** (map fog) — present in the mockup only as a disabled
"later" tab. **Call Flows** is a top-level library (one flow → many numbers, ticket 07); editing a flow opens
this same rule form.

### SMS — Messages inbox (decided; realizes ticket 08 UX)
Two-pane inbox: thread list (derived by `(number_id, caller_id)`, unread from `last_read_at`) + conversation
(interleaved in/out bubbles). **Polling**, no WebSockets (ARCH decision 13 / ticket 08). Reply always goes out
on the **thread's number** (no picker). Numbers without 10DLC → **disabled composer + "not registered" badge**
(ticket 08 gate, bridge from ticket 12); opted-out (STOP) contacts hard-block send. Both directions relay to GHL.

### AI Agents — dedicated library (decided; surface only, config is ticket 11)
Top-level **AI Agents** section = a library of reusable agents (name, persona gist, voice, "used in" flows).
An agent is **never bound to a number** — it is **selected from a dropdown inside a flow's menu/routing step**
(ticket 07/06). This is the "simple path" protection: deep agent config (ticket 11) lives in its own section so
the number/flow forms stay uncluttered.

### In-call operator UI (coordination only → ticket 13)
Mockup shows a **persistent live-call bar** (answer/transfer/hang-up) as a placeholder for where the WebRTC
softphone surfaces. Full transport/UX is **ticket 13's** call; this ticket only reserves the slot.

### Settings (minor additive note, not a decision)
Settings page gains: BulkVS trunk + ARI **`/health/telephony`** status (ticket 09, non-gating) and the BulkVS
inbound-message webhook URL — implementation detail, listed so it isn't lost.

### Fog surfaced (added to map "Not yet specified")
- **Visual flow-builder canvas** — already in fog (ticket 06); this ticket confirms v1 ships the form instead.
- **Dashboard platform-awareness** — optional BulkVS/Asterisk provider breakdown + SMS tiles on the existing
  analytics dashboard. Explicitly out of this ticket's IA decision; noted for later.

### Dependencies / unblocks
Nothing is blocked by 10. It consumes 06 (flow vocabulary for the form), 07 (number hub / split identity /
lifecycle), 05 (provider-segregated call log), 08 (SMS inbox), and reserves surfaces for 11 (agent config) and
13 (in-call UI). No new tickets required; no scope changes.

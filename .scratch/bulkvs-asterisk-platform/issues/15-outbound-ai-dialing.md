# Manual operator outbound calling

Type: grilling
Status: resolved (2026-07-22 — grilling; reframed from "Outbound AI dialing" to human-placed outbound; AI dialing graduated to fog. Additive, no regression to trunk/Twilio/SignalWire/GHL paths)
Assignee: svillahermosa
Claimed by: wayfinder session 2026-07-22 (ticket 15)
Blocked by: 11

## Question

Graduated from [ticket 11](11-ai-agent-config-and-runtime.md) as "Outbound AI dialing / campaign dialing"
— an AI agent that *places* calls plus the campaign machinery around it (contact lists, pacing, AMD, retry,
TCPA-at-scale).

**Reframed on resolution (2026-07-22).** Grilling surfaced that the actual near-term need is **manual
outbound calling** — a *human* operator placing a call from the platform, choosing which owned number to call
*from*, to any customer, and talking live. **AI placing the calls is "maybe later," explicitly not mandatory
for now.** The AI-campaign scope is deferred wholesale to a fresh fog entry (see map "Not yet specified"); this
ticket resolves the human-placed outbound call.

Original (now-deferred) AI questions, preserved for the fog entry: campaign model (tables vs reuse
`campaigns`); contact lists + per-contact state; dial pacing/concurrency vs inbound; TCPA/consent for AI-placed
voice; answering-machine detection; cost guardrails at scale.

## Answer

**Resolved 2026-07-22 via `/grilling`.** Manual operator outbound calling is a **thin additive delta on
[ticket 13](13-in-platform-webrtc-calling.md)** — the operator softphone that ticket already designed. Ticket
13's Q4 already specified *placing* a call ("browser INVITEs into Stasis at an 'originate' address → backend
originates the trunk leg, bridges") and its UI spec already includes an "Idle / dialer — number entry +
place-call." So the bridge mechanics, data model, and recording pipeline for a human-placed outbound call are
**already decided by tickets 13 / 04 / 05**. The only genuine net-new decisions are **which number the call
goes out from (caller ID)**, **how recording consent is handled outside the flow graph**, and **what
compliance guardrails apply**. Everything is additive and flag-gated under the existing `ASTERISK_ENABLED`
module; nothing touches the IP-locked BulkVS trunk ingest path, Twilio/SignalWire, or GHL relay.

### Decision log

1. **Use-case posture — warm follow-up only.** Outbound is for calling people with an existing business
   relationship / prior contact (returned missed calls, lead callbacks, confirmations). **No cold lists, no
   purchased/scraped targets.** This keeps TCPA exposure low and removes the need for DNC-scrubbing and
   prior-express-written-consent scaffolding that AI cold-calling would require. (This posture also governs the
   deferred AI-outbound effort — see fog.)

2. **Scope pivot — manual (human-placed) outbound now; AI dialing deferred.** The near-term, mandatory need is
   a human operator placing calls. **AI *placing* calls, campaigns, contact-list pacing, AMD, retry policy, and
   the `outbound_calls` queue table are all deferred to a single fresh fog entry** — none are needed for a human
   dialing live and immediately. (Note: an earlier grilling turn had tentatively chosen a new `outbound_calls`
   table; that decision belongs to the **deferred AI path** — a live human call is immediate origination with
   no queue/schedule/retry, so **v1 needs no outbound queue table**.)

3. **Mechanics reuse ticket 13 / 04 / 05 — no new schema.** Operator dials via the ticket-13 `chan_pjsip`
   WebRTC softphone → SIP.js INVITEs into Stasis → the ticket-05 backend ARI consumer **originates the BulkVS
   trunk leg (ARI `originate`, proven in ticket 04) and bridges** the two legs. The call is one `calls` row,
   `provider_id=asterisk`, **`direction='outbound'`** (ticket 05), one channel per leg under `Linkedid`. It
   reuses the existing recording → transcription → analysis → GHL pipeline unchanged. `campaign_id` attributes
   via the **from-number** (ticket 05 already specified this for outbound). **No migration beyond what ticket 05
   defined.**

4. **From-number / caller ID — picker + remembered default (the real net-new delta).** The softphone dialer
   shows a **dropdown of the org's active BulkVS-owned numbers** (`media_provider=asterisk`); the operator picks
   which to present as caller ID per call, with the **last-used number remembered as the default**. Constraint
   (ticket 01): the presented CLI must be a **DID OWEN actually owns** on the BulkVS account — presenting a
   foreign/arbitrary number is **out of scope** (spoofing; BulkVS rejects/overrides). "Call from any number" =
   "from any number we own." (Rejected: single org-wide default — contradicts the ask; per-operator bound
   number — ties identity to a number needlessly.)

5. **Recording + consent — auto notice before bridge.** Manual outbound does **not** traverse the ticket-06
   flow graph (inbound-only, per ticket 13), so ticket-06's `play`-node consent notice never fires. FL is
   all-party consent. v1: outbound **records by default**; when the callee answers, the backend runs a small
   **pre-bridge ARI `play` step** ("this call may be recorded") to the callee **before bridging the operator
   in**. This is a standalone imperative ARI step outside the interpreter — the outbound analogue of ticket
   06's entry-consent `play`. Deeper per-call consent handling stays in the existing fog item ("per-call
   recording-consent for Asterisk-controlled legs"). (Rejected: rely on operator verbal disclosure — less
   reliable; record-off-by-default — loses transcription/analysis/GHL on most outbound calls.)

6. **Compliance guardrails — soft, non-blocking warnings only.** No hard blocks. The dialer surfaces a
   **non-blocking warning** if (a) the callee is on the **`sms_opt_outs`** list (ticket 08 — reused as a
   cross-channel opt-out *signal*, not an enforcement gate) or (b) the call is outside **8am–9pm in the
   callee's likely timezone** (derived from area code; best-effort). The operator may proceed. No new voice-DNC
   table, no hard calling-hours enforcement — appropriate for human-judged warm follow-up in a single-org
   internal tool. (Rejected: hard enforcement — overbuilt, needs reliable per-contact TZ + a DNC list that
   doesn't exist; nothing at all — cheap to surface the opt-out signal we already have.)

7. **Entry points — dialer + "call" action on records.** Two ways to initiate: the ticket-13 softphone
   **dialer** (free number entry) and a **"call" action button on caller/contact records** (and the missed-call
   surface from ticket 07). Both open the same softphone flow with the from-number picker (Q4). Fills the
   dialer/live-call-bar slot ticket 10 already reserved.

### Confirmed facts (not decisions — verified against tickets 01/04/05/13)
- **Origination is proven.** Ticket 04 originated an outbound call via ARI on real infra (Asterisk 22.10.1,
  Host already registered to BulkVS). RURI is `+E.164` (ticket 04 gotcha, already folded into ticket 06).
- **Data model needs nothing new.** Ticket 05 already models outbound: same `call_events`→`calls` projection,
  `direction='outbound'`, `campaign_id` via `from_number`, `_is_inbound` drop stays Twilio-only. Recording
  reuses the existing table + pipeline.
- **Bridge/transcode already cleared.** Ticket 13 confirmed mixed DTLS-SRTP (operator) ↔ plain-RTP (trunk)
  bridging and Opus↔ulaw transcode are fine.

### Downstream impact / feedback
- **Ticket 10** (operator UX): add the **from-number picker** to the softphone dialer and a **"call" action**
  on caller/contact + missed-call records. No IA change — fills the reserved dialer/live-call-bar slot. (Note
  surfaced here; 10 already resolved.)
- **No change to tickets 05/06/13** — this ticket consumes their decisions; the pre-bridge consent `play` is a
  new imperative ARI step, not a flow-graph node, so ticket 06 is untouched.

### Fog surfaced (added to map "Not yet specified")
- **AI outbound dialing / campaign dialing (the entire deferred effort)** — an AI agent that *places* calls,
  plus: campaign model (contact list + agent + schedule + retry vs. reuse `campaigns`); an `outbound_calls`
  queue table with per-contact state; contact-list sourcing (existing `callers` / GHL) + dedup; dial
  pacing/concurrency without starving inbound answering; **AMD** (voicemail vs. human) + agent-on-machine
  behavior; **TCPA/consent for AI-placed voice** (artificial/prerecorded-voice rules — the hard constraint);
  cost/rate guardrails at campaign scale. Reuses ticket 11's `VoiceAgentSession` runtime seam and this ticket's
  origination + from-number + consent-notice patterns when it lands. Returns as a fresh effort when AI outbound
  becomes a priority.

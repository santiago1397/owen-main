# In-platform calling — WebRTC softphone leg into Asterisk

Type: grilling
Status: resolved (2026-07-22 — grilling; additive WebRTC seam, no regression to trunk/Twilio/SignalWire/GHL paths)
Assignee: svillahermosa
Claimed by: wayfinder session 2026-07-22 (ticket 13)
Blocked by: 06, 09

## Question

Surfaced resolving [ticket 05](05-asterisk-as-provider-data-model.md): the operator must be able to **place and
receive calls directly in the web UI** ("handle from the same platform if needed"), and hand a live call off to
another number, an AI agent, or a human operator. Data-model-wise this is neutral — the operator is just another
channel under the call's `Linkedid`, one `calls` row (settled in 05) — but the **transport, infra, and UX are new**.

- **WebRTC endpoint:** the browser registers a WebRTC softphone into Asterisk. Which transport — `chan_pjsip`
  WebRTC (wss + DTLS-SRTP) vs. `chan_websocket`/AudioSocket (ticket 02 noted these for the AI path)? One shared
  media seam or two?
- **Signaling/registration:** how does a browser authenticate + register to Asterisk (short-lived ARI-minted
  credentials? per-operator SIP endpoint?), given ARI is localhost-bound and SIP/5060 is IP-locked to BulkVS SBCs
  (ticket 04/09). This needs a **separate secured WebRTC transport** — reconcile with ticket 09's security model.
- **Call control from the UI:** how the front-end drives answer/hangup/hold/transfer — via our backend ARI
  consumer (ticket 05) issuing ARI ops, not the browser talking to ARI directly.
- **Redirect/transfer targets:** operator → another DID, → an AI agent handler node (ticket 06 graph / ticket 11
  runtime), → another operator. How transfer maps onto the flow graph vs. an ad-hoc ARI bridge operation.
- **Presence/routing:** how an inbound flow reaches "ring the operator's browser" (a flow node), operator
  availability, and fallback (no answer → voicemail/agent).
- **TLS/media:** DTLS-SRTP for the browser leg even though the BulkVS trunk leg is plain RTP (ticket 01: no
  TLS/SRTP on the trunk) — confirm the mixed-security bridge is fine.

Use `/grilling` + `/domain-modeling`; likely also needs a `/prototype` for the in-call UI (coordinate with
ticket 10). Depends on the flow-graph vocabulary (06) for transfer/handoff nodes and the infra/security model (09)
for the WebRTC transport.

## Answer

**Resolved 2026-07-22 via `/grilling`.** The operator softphone is an **additive WebRTC seam** into the
existing native Asterisk: the browser becomes a real per-operator `chan_pjsip` WebRTC endpoint (one channel
under the call's `Linkedid`, per ticket 05), signaling rides Traefik-fronted `wss`, media is DTLS-SRTP direct
to the existing RTP range with a coturn fallback, and **all call control above the browser's own leg is
backend ARI**. Nothing here touches the IP-locked BulkVS trunk path, the Twilio/SignalWire ingest, or GHL
relay — it's a new transport + a per-operator endpoint + presence state, flag-gated under the same
`ASTERISK_ENABLED` module (ticket 09).

### Decision log (8 questions, all resolved with the recommended option)

1. **Transport seam — `chan_pjsip` WebRTC, separate from the AI path.** The operator browser leg rides
   `chan_pjsip` WebRTC (SIP-over-WebSocket `wss` + **DTLS-SRTP** media) via a JS SIP UA, making it a full SIP
   endpoint that appears as an ordinary channel under `Linkedid` (ticket 05). This is a **distinct media seam**
   from the AI external-media path (`chan_websocket`/AudioSocket, ticket 02/03) — the two solve different
   problems (human bidirectional SIP endpoint vs. server↔provider audio tap) and are not merged.
2. **Auth/registration — static per-operator PJSIP endpoint, backend-minted session creds.** Each operator
   has a `chan_pjsip` WebRTC endpoint (e.g. `operator_alice`) defined in the in-repo `asterisk/pjsip.conf`
   (ticket 09). On app-login the FastAPI backend hands the browser a **session-scoped SIP password** (rotated
   per login), which SIP.js uses to REGISTER over `wss`. The **real auth gate is the existing app login**; SIP
   secrets are rendered from a store, never committed. (Rejected: per-call ARI-ephemeral creds — needless
   dynamic-reload machinery at single-org scale; shared endpoint — loses per-operator identity needed for
   routing + audit.)
3. **wss exposure + media path — Traefik-fronted signaling; media direct to existing RTP range.**
   - *Signaling:* the browser's `wss` terminates at **Traefik** (the existing TLS edge, owns 443/certs) →
     plain `ws`/loopback to Asterisk's WebSocket. **No new public cert lifecycle**; Asterisk's WS stays
     loopback/bridge-bound, consistent with ARI's treatment (ticket 09). One new public surface: a `wss` route
     on a Traefik-owned domain.
   - *Media:* WebRTC media is peer-to-peer UDP and **cannot** traverse Traefik. Browser DTLS-SRTP flows
     **directly to the existing `10000–10200/udp` RTP range** (ticket 09, session-validated — no new range).
     Asterisk configured `icesupport=yes` advertising the **VPS public IP** as the host ICE candidate; the
     media DTLS cert is generated on the host and rendered at deploy (same convention as other secrets).
4. **Control split — browser owns only its own leg; everything else is backend ARI.** SIP.js handles the
   operator leg's intrinsic SIP signaling only: INVITE (place), 200-OK (answer incoming), BYE (hang up its own
   leg). **All bridging, hold (holding-bridge + MoH), and transfer are backend ARI operations** issued by the
   ticket-05 ARI consumer, triggered by the UI calling **our FastAPI** (e.g. `POST /calls/{id}/transfer`) —
   **the browser never speaks ARI** (ARI stays localhost-bound, ticket 09). Place = browser INVITEs into
   Stasis at an "originate" address → backend sees `StasisStart`, originates the trunk leg, bridges. Receive =
   an inbound flow's operator-`dial` (Q6) originates a leg to the operator endpoint; browser rings + answers
   via SIP.
5. **Transfer — imperative ARI ops, flow graph stays inbound-only; blind for v1.** The ticket-06 flow graph
   governs **inbound automated routing**; once a human is live, transfer is an **imperative ARI operation**,
   not flow traversal. Three target kinds, all backend-driven: **DID** (originate trunk leg + bridge),
   **operator** (originate to that operator's PJSIP endpoint + bridge), **AI agent** (invoke ticket 11's
   `ai_agent` runtime entry directly, handing off the operator's channel — *not* a raw bridge, since the agent
   needs the runtime seam). **Blind transfer (drop-and-go) ships v1; attended (consult-bridge) is a fast
   follow.** (Rejected: modelling transfer as flow-node jumps — over-engineers a live imperative action through
   an interpreter built for automated inbound.)
6. **Presence/routing — reuse `dial` with an operator-target kind; dual-signal presence.** An inbound flow
   reaches a human by a ticket-06 **`dial` node whose target is an operator endpoint** (`operator:alice`
   individual or `operator:sales` group) — no new node type; `dial`'s single/ring-all/sequential strategies +
   `answered`/`no_answer`/`busy_failed` ports already cover it, and no-answer falls through to
   `default_fallback` (voicemail/agent). **Availability = two signals AND-ed:** (1) the operator's browser is
   *registered* (Asterisk device/registration state) and (2) an **app-level available/busy toggle** (auto-busy
   while on a call). Nobody available → fast `no_answer` → fallback (no dead air). ⇒ **schema-extension note
   back to ticket 06:** extend `dial` target vocabulary to accept operator endpoints (individual + group)
   alongside DIDs. (Rejected: a dedicated `ring_browser` node — duplicates `dial`'s strategy/port machinery.)
7. **TURN — coturn in v1 (443/TLS relay), backend-minted short-lived creds.** DTLS-SRTP media over UDP to
   `10000–10200` fails silently (signaling up, no audio) behind corporate/home firewalls that block outbound
   UDP. **coturn is stood up in v1**, able to relay over **TCP/TLS on 443** for near-universal traversal; TURN
   creds are **minted short-lived by the backend** alongside the SIP creds (Q2). coturn follows ticket 09's
   native-service + rendered-secret mold with its own additive UFW rules. ⇒ **addition to ticket 09's infra
   scope** (see note appended there). (Rejected: host-candidates-only with "add TURN if needed" — ships a
   softphone that silently fails for a coin-flip of operator networks.)
8. **In-call UI — specify states, no separate prototype.** The behavior is now tightly pinned by Q1–Q7, and
   ticket 10 already locked the IA and **reserved the live-call-bar slot** + house style. No `/prototype`; the
   softphone UI is a direct rendering of the state machine (below). A cheap follow-up prototype is fine later
   if a specific interaction turns out genuinely open at build time.

### Confirmed facts (verified against tickets 01/02/05/06/09 + how Asterisk works — not decisions)
- **Mixed-security bridge is a non-issue.** Asterisk is a B2BUA with media anchored (`direct_media=no`,
  ticket 02): it terminates DTLS-SRTP on the browser leg and plain RTP on the trunk leg independently; the
  mixing bridge re-originates media between them. Browser-encrypted + trunk-plaintext (ticket 01: trunk has no
  TLS/SRTP) in one call is standard and fine.
- **Opus↔ulaw transcoding.** Browsers negotiate Opus; the BulkVS trunk is ulaw (ticket 01). Asterisk
  transcodes (needs `codec_opus`); negligible CPU at single-org scale. Config note for the `asterisk/` dir.
- **Operator calls reuse the existing data + recording pipeline.** The operator leg is another channel under
  `Linkedid` → one `calls` row, `provider_id = asterisk` (ticket 05). Recording uses ticket 06's `record`
  modifier / bridge recording → same recording + transcription + analysis + GHL pipeline. Net-new persistence
  is only the per-operator SIP endpoint config + the presence/availability state (+ session SIP/TURN creds).
- **Library — SIP.js** (over JsSIP), for the TypeScript + React fit with the existing SPA.

### Softphone UI states (spec for implementation; renders into ticket 10's reserved live-call-bar slot)
- **Idle / dialer** — number entry + place-call; the app-level **available/busy toggle** (Q6) lives here.
- **Incoming-call toast** — caller id + flow/number context; **answer / decline**.
- **Active-call bar** — mute · hold · **blind-transfer to [DID | operator | AI agent]** (Q5) · hang-up;
  shows the live call's identity (linked to the ticket-05 `calls` row / existing `CallDrawer`).
- **Transferring** — transient state while the backend ARI op runs.
All states use ticket 10's dark house style; polling for presence/roster (no new WS beyond the SIP `wss`).

### Downstream impact / feedback
- **Ticket 06** (flow-graph): `dial` target vocabulary extended to include operator endpoints (individual +
  group). Note appended to ticket 06's answer (schema addition surfaced here; 06 already closed).
- **Ticket 09** (infra/security): **coturn** added as a second native telephony service (UDP 3478 / TLS 5349 +
  relay range, additive UFW), and the Traefik `wss` route + Asterisk WebSocket (loopback/bridge-bound) + media
  DTLS cert rendering join its deploy/secret story. Note appended to ticket 09's answer.
- **Ticket 11** (AI-agent runtime): the "transfer to AI agent" target (Q5) invokes 11's `ai_agent` runtime
  entry point directly — 11 should expose an entry that accepts a handed-off live channel, not only a
  flow-node entry.
- **Ticket 10** (operator UX): the reserved live-call-bar slot is filled by the state spec above; no IA change.

### Fog surfaced (added to map "Not yet specified")
- **Attended (consult) transfer** — v1 ships blind; attended needs a consult-bridge dance (extra UI + ARI
  choreography), deferred as a fast follow.

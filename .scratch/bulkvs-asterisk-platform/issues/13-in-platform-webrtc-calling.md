# In-platform calling — WebRTC softphone leg into Asterisk

Type: grilling
Status: open
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

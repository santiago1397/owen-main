# 13 — In-platform calling — WebRTC softphone leg

**What to build:** The operator answers a platform call in the browser, with hold/transfer driven safely by the backend and audio that survives restrictive firewalls.

**Blocked by:** 01, 04, 07

**Status:** ready-for-agent

- [ ] Per-operator `chan_pjsip` WebRTC endpoint (SIP.js, `wss`+DTLS-SRTP), one channel under `Linkedid`; separate seam from the AI external-media path
- [ ] Signaling `wss` fronted by Traefik; media DTLS-SRTP direct to the existing `10000-10200/udp` range; coturn added (TLS relay over 443); session SIP password minted by the backend at app-login
- [ ] SIP.js drives only its own leg; all bridge/hold/blind-transfer go through backend ARI (never browser->ARI)
- [ ] Transfer targets: DID / operator / AI-agent runtime (blind for v1); presence via `dial` operator-target + app toggle; no-answer -> `default_fallback`
- [ ] In-call bar UI fills ticket-06's reserved slot; a missed platform call is captured and answerable in-app

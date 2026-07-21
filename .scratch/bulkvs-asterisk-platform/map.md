<!-- wayfinder:map -->
# Map: BulkVS + Asterisk number-management platform

## Destination

Convert the existing call-**attribution** platform into an active **number-management platform** on
**BulkVS (DIDs + SIP trunk) + Asterisk (media brain, via ARI)** — where a single-org operator can, through
an easy UI: provision/assign BulkVS numbers; define per-number call behavior (forward / record / voicemail /
business hours / IVR menu) via declarative rules stored as a flow-graph; send and receive SMS; and run **AI
voice agents** that answer and place calls — all integrated with the existing recording / transcription /
attribution / GHL pipeline.

Reaching the end = a **spec + locked decisions** ready to hand to implementation (this map plans; it does not build).

## Notes

- **Domain:** the existing system is documented in [`ARCHITECTURE.md`](../../ARCHITECTURE.md) — event-sourced
  `call_events` (truth) → `calls` (projection), Postgres job queue, single-replica `worker`, pluggable
  providers (Twilio live, SignalWire planned), local-disk recordings, LLM analysis, GHL relay. Deploy = the
  Traefik/Docker + native-host-Postgres convention. Asterisk becomes a **third provider**, native on the host.
- **Mode:** planning. Produce decisions/spec, not deliverables. The one `task` ticket exists only to unblock
  decisions (prove BulkVS↔Asterisk works so infra/data-model choices are grounded in reality).
- **Skills to consult per ticket:** `/grilling` + `/domain-modeling` for design tickets, `/research` for
  research tickets, `/prototype` for the UX ticket. Never resolve more than one ticket per session (research excepted).
- **Standing preference:** easiest possible operator UX — "best of QUO/Twilio/SignalWire." No-code where feasible.
- **HARD CONSTRAINT (applies to every ticket):** the **live Twilio, SignalWire, and GHL integrations must keep
  working unchanged** after these new modules land. This is additive work. Do not break, rewrite, or regress the
  existing ingestion / recording / analysis / GHL-relay paths unless a change is genuinely necessary or a clear
  optimization — and if so, flag it explicitly in the ticket rather than doing it silently. Asterisk/BulkVS is a
  new provider *alongside* them, never a replacement (porting/cutover stays out of scope).

## Decisions so far

<!-- one line per closed ticket; zoom the link for detail -->

- **Media brain** — Asterisk (native, existing) via ARI from the backend; BulkVS SIP trunk → Asterisk; no CPaaS in the voice media path; AI path (ARI external-media) kept reachable. *(named while charting)*
- **Tenancy** — single-org internal tool; no per-tenant isolation or billing. *(named while charting)*
- **Call-flow authoring** — declarative rule forms now, persisted as a flow-graph from day one so a visual builder layers on later without rewrite; flow executes on ARI. *(named while charting)*
- **Twilio** — coexist; Asterisk added as a third provider feeding the same event-sourced tables. *(named while charting)*
- [Asterisk / ARI capabilities + trunk config](issues/02-asterisk-ari-capabilities.md) — ARI covers every needed primitive (answer/play/record/bridge-dial/DTMF/originate; external-media 16.6+, prefer `chan_websocket` 20.16+/AudioSocket 18+ for AI). BulkVS = IP-auth `chan_pjsip` endpoint+aor+identify, `direct_media=no`, ulaw; record the bridge; Docker→host ARI via `host.docker.internal`, bind localhost. **Verify installed Asterisk version early.**
- [BulkVS platform + API capabilities](issues/01-bulkvs-platform-and-api.md) — REST `portal.bulkvs.com/api/v1.0` (Basic auth): `/tnRecord` list/route, `/orderTn`+`/exchanges` search/buy, `/trunkGroups`; **no inventory webhook (poll)**. Trunk = UDP/5060 IP-auth, ulaw/RFC2833, 11-digit RURI, **no TLS/SRTP**, IPs 162.249.171.198/76.8.29.198/69.12.88.198/199.255.157.198. SMS via `messageSend`+inbound webhook (src 52.206.134.245/192.9.236.42); **10DLC blocks outbound SMS**. **No CDR API → source calls from Asterisk.**
- [AI voice-agent architecture options](issues/03-ai-voice-agent-architecture.md) — recommended default **OpenAI Realtime bridged over external-media** (on-box control/recording, native barge-in, ~$0.06–0.11/min); Vapi-over-SIP as fast-start pilot; DIY (OpenAI STT+LLM → MiniMax TTS) as escape hatch. **MiniMax = TTS-only.** Pluggable seam mirrors the existing `TranscriptionEngine` pattern.

## Not yet specified

<!-- in-scope fog; graduates into tickets as the frontier advances -->

- Visual flow-builder canvas (the graph schema is ticketed; the builder UI is not).
- Outbound AI dialing / campaign dialing (as opposed to a single AI agent answering).
- STT/TTS/LLM provider *tuning* + barge-in/latency budgets — the architecture is chosen (ticket 03: OpenAI Realtime default, seam design); per-agent tuning is downstream of ticket 11.
- Per-call recording-consent handling for Asterisk-controlled legs (FL all-party; noted in ARCHITECTURE.md decision 17).
- Asterisk HA / failover / concurrency ceilings.
- MMS specifics (vs SMS-only).
- Voicemail storage/transcription reuse of the existing recording+transcription pipeline (likely reuses it; confirm when flow schema lands).

## Out of scope

<!-- ruled beyond the destination; never graduates -->

- **Multi-tenant SaaS + usage billing** — destination is a single-org tool. Returns only as a fresh effort.
- **Full Twilio number porting / cutover / decommission** — coexistence only; migration is a separate future effort.

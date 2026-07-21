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

## Not yet specified

<!-- in-scope fog; graduates into tickets as the frontier advances -->

- Visual flow-builder canvas (the graph schema is ticketed; the builder UI is not).
- Outbound AI dialing / campaign dialing (as opposed to a single AI agent answering).
- Specific STT/TTS/LLM provider selection + tuning (Vapi vs OpenAI Realtime vs MiniMax), barge-in and latency budgets — downstream of the AI-architecture research.
- Per-call recording-consent handling for Asterisk-controlled legs (FL all-party; noted in ARCHITECTURE.md decision 17).
- Asterisk HA / failover / concurrency ceilings.
- MMS specifics (vs SMS-only).
- Voicemail storage/transcription reuse of the existing recording+transcription pipeline (likely reuses it; confirm when flow schema lands).

## Out of scope

<!-- ruled beyond the destination; never graduates -->

- **Multi-tenant SaaS + usage billing** — destination is a single-org tool. Returns only as a fresh effort.
- **Full Twilio number porting / cutover / decommission** — coexistence only; migration is a separate future effort.

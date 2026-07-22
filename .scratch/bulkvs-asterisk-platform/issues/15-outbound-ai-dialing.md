# Outbound AI dialing / campaign dialing

Type: grilling
Status: open
Blocked by: 11

## Question

Graduated from [ticket 11](11-ai-agent-config-and-runtime.md): everything in 11 is **inbound** (an AI
agent answering a call routed to an `ai_agent` flow node). This ticket designs **outbound** — an AI agent
that *places* calls, and the campaign machinery around it. ARI `originate` is already proven on real infra
(ticket 04) and the agent config + `VoiceAgentSession` runtime seam is settled (ticket 11); this ticket
reuses both. The net-new, statable questions:

- **Campaign model:** what is an outbound campaign (contact list + agent + schedule + retry policy), and
  how does an agent attach to a campaign vs. to an inbound flow node? New tables vs. reuse `campaigns`?
- **Contact lists:** where do target numbers come from (manual upload, GHL pull, a query over existing
  `callers`?), dedup, and per-contact state (queued/attempted/connected/done/opted-out).
- **Dial pacing:** how fast to dial (concurrency ceiling, gap between calls), and how outbound calls flow
  through the same worker/ARI path as inbound without starving inbound answering.
- **Compliance (the hard part):** **TCPA/consent** — calling hours by timezone, prior-express-consent
  tracking, honoring the `sms_opt_outs`/DNC equivalent for voice, and the legal posture for AI-placed
  calls. This may constrain the whole feature.
- **Answering-machine detection (AMD):** voicemail vs. human — Asterisk AMD vs. provider vs. the agent
  itself deciding; what the agent does on machine (leave a message? hang up?).
- **Outcome recording:** reuse the ticket-05 `calls`/`call_events` model with `direction='outbound'`
  (ticket 05 already notes outbound attributes `campaign_id` via `from_number`); reuse ticket-11
  transcript/analysis/GHL path.
- **Cost/rate guardrails** at campaign scale (beyond ticket 11's per-call caps).

Use `/grilling` + `/domain-modeling`. Likely graduates further fog (predictive dialing, A/B agent
testing, retry-strategy tuning).

## Answer

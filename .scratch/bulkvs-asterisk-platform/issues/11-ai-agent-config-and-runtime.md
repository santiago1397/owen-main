# AI-agent config + runtime model

Type: grilling
Status: open
Blocked by: 03, 06

## Question

Given the architecture chosen in ticket 03, define how an AI agent is configured and how it runs — the deepest
part of the map.

- **Config:** what defines an agent (prompt/persona, voice, tools/actions, knowledge, guardrails), and how it
  attaches to a number/flow as a handler node in the ticket-06 graph.
- **Runtime:** the audio path (ARI external-media ↔ provider), turn-taking/barge-in, how a call transitions
  between rule-based nodes and the agent (e.g. IVR "press 2 to talk to the assistant"), and agent→human handoff/transfer.
- **Provider seam:** a pluggable interface (mirroring `TranscriptionEngine`) so Vapi/OpenAI/MiniMax are swappable.
- **Data:** how agent turns/transcripts/outcomes record into the call model + analysis/attribution.
- **Cost/latency guardrails** and failure fallback (agent errors → voicemail?).

Use `/grilling` + `/domain-modeling`. Likely graduates fog (outbound AI dialing, provider tuning) into new tickets.

## Answer

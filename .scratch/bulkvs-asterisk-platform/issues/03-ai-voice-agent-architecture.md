# AI voice-agent architecture options

Type: research
Status: open
Blocked by: —

## Question

Lay out the viable architectures for AI agents that answer/place calls on this stack, so a later design ticket
can choose. Compare on latency, barge-in quality, cost, and integration effort against **Asterisk + ARI**:

- **DIY on ARI external-media** — stream call audio to our backend; pipe to STT → LLM → TTS (OpenAI, MiniMax)
  and inject audio back. What's the real end-to-end latency, and what does barge-in/turn-taking require?
- **OpenAI Realtime API (speech-to-speech)** — bridging Asterisk external-media to a realtime websocket.
- **Vapi (or similar) over SIP** — route AI-agent calls from Asterisk out to Vapi via SIP; Vapi owns the media/AI
  loop. Trade-off: less control + per-min cost vs. far less to build. Can BulkVS→Asterisk→Vapi be done cleanly?
- Which providers the user named to start (**Vapi / OpenAI / MiniMax**) fit which role (STT / LLM / TTS), and
  what a provider-pluggable seam would look like (mirroring the existing `TranscriptionEngine` pattern).

Output: a comparison table + a recommended default, feeding ticket 11 (AI-agent config + runtime).

## Findings

<!-- resolved by /research subagent; link the captured research file here -->

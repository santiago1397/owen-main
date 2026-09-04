# 12 — AI agent openai_realtime runtime

**What to build:** A real caller has a natural spoken conversation with a live AI agent that can capture a lead, send an SMS, transfer, or end the call -- and never leaves the caller in dead air on failure.

**Blocked by:** 11

**Status:** ready-for-agent

- [ ] `openai_realtime` engine bridges AudioSocket/TCP <-> OpenAI Realtime <-> the call bridge; server-VAD barge-in with eager outbound-buffer flush
- [ ] Fixed tool registry with per-agent toggles: flow-exit `transfer`/`end_call` (-> node ports), in-call `capture_lead`/`send_sms`; no arbitrary LLM HTTP
- [ ] Transcript written inline to `transcriptions` (speaker-labeled; agent legs skip post-call STT); bridge WAV via `record`; `capture_lead` -> `call_analysis.captured` authoritative
- [ ] Guardrails per agent (`max_call_seconds`/`max_silence_seconds`/model tier); any error -> `failed` port -> `default_fallback` after 1 WS-reconnect retry

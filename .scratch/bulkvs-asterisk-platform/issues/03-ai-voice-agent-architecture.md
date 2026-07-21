# AI voice-agent architecture options

Type: research
Status: resolved
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

Resolved by `/research` subagent. Three architectures compared (latency / barge-in / cost / effort / data-locality).

**Recommended default: OpenAI Realtime API bridged over Asterisk external-media** (WebSocket transport, G.711
µ-law to avoid transcoding). Rationale: keeps BulkVS + Asterisk + our backend as source of truth (recording,
attribution, analysis, GHL relay stay **on-box**), gives production-grade **barge-in/VAD/turn-taking + function
calling for free** (the hardest, riskiest part of DIY), single vendor, ~$0.06–0.11/min flagship (~$0.02–0.05 mini),
~300–500ms response. 
- **Fast-start/pilot:** Vapi over SIP (BulkVS→Asterisk→`sip.vapi.ai`, whitelist Asterisk public IP, G.711-only) —
  working agent in days, but media+transcripts live at Vapi and must be relayed back (conflicts with on-box core).
- **Escape hatch:** DIY pipeline OpenAI STT+LLM → **MiniMax TTS** behind the same seam, for cost/voice tuning.
  **MiniMax is TTS-only — do NOT use it for STT.**

**Build facts:** external-media gotcha #1 = read `UNICASTRTP_LOCAL_ADDRESS/PORT` back over ARI to know where to
inject return audio; watch a reported slin↔µ-law negotiation bug (validate on installed version). AudioSocket (TCP,
320-byte/20ms frames) is the lower-friction transport vs raw RTP. Barge-in in DIY = you build VAD + buffer-flush.
OpenAI Realtime: WS is the correct transport for server/SIP bridging; PCM16@24k or G.711@8k; native server-VAD +
interruption + tools.

**Pluggable seam:** mirror the existing `backend/app/analysis/transcription.py` `Protocol`+registry+`dummy`-default
pattern → a `VoiceAgentSession` Protocol with engines `dummy` / `openai_realtime` / `diy_pipeline` / `vapi_sip`,
selected by a `VOICE_AGENT_ENGINE` config switch; sub-provider STT/LLM/TTS as their own small Protocols. → feeds
ticket 11 (AI-agent config + runtime).

**Caveats:** OpenAI Realtime pricing + reported ~30-min session cap are third-party-sourced (verify on official
pages); DIY latency band is an estimate; Vapi BYO-trunk inbound source-IP match + G.711-only from the Plivo/Twilio
docs (confirm for a BulkVS-fronted Asterisk origination).

<details>
<summary>Full research report</summary>

**1. DIY on ARI external-media** — StasisStart → mixing bridge + externalMedia channel (`rtp`/`slin16`) to backend
UDP host:port; read back `UNICASTRTP_LOCAL_ADDRESS/PORT` for return audio (#1 no-audio gotcha). AudioSocket (TCP,
8k/16-bit/mono, 320B=20ms frames) preferred over RTP. Naive sequential chain ~2–4s; streaming concurrency gets
~800ms–1.5s realistic (250ms only in a Groq+Cartesia case study). Barge-in = you build VAD (Silero/WebRTC) + flush
TTS buffer + stop ARI playback; hardest part, main quality risk.

**2. OpenAI Realtime (bridged)** — external-media/AudioSocket ↔ FastAPI ↔ Realtime WebSocket (WS is OpenAI's
recommended transport for server/SIP; native SIP also exists). `input_audio_buffer.append` PCM16; receive
`response.output_audio.delta`. Formats PCM16@24k mono LE, or G.711 µ/A-law@8k (transcode-free telephony fit).
~300–500ms latency. Native server-VAD/turn detection/interruption + function calling (`session.update` tools).
Cost token-based: gpt-realtime ~$32/$64 per 1M audio in/out, mini ~$10/$20 → ~$0.06–0.11/min flagship, $0.02–0.05
mini w/ caching (third-party; verify). Reported ~30-min session cap (unverified).

**3. Vapi over SIP** — inbound+outbound on one credential; BYO-SIP works. Inbound identified by **source-IP CIDR
match** (no hostname) → whitelist Asterisk public IP. G.711 µ/A-law only; UDP5060/TLS5061/SRTP-on-request.
BulkVS→Asterisk→`sip.vapi.ai` clean; friction = IP whitelist + caller-ID/header preservation. Cost ~$0.05/min Vapi
fee + components → ~$0.15–0.33/min all-in. Trade-off: media+AI loop+recordings at Vapi, must relay back — conflicts
with on-box recording/attribution/GHL.

**Provider→role:** STT: OpenAI ✅ (already wired), MiniMax ❌ (TTS-only), Vapi orchestrates. LLM: OpenAI ✅. TTS:
MiniMax ✅ strong/low-latency (speech-2.6/2.8, WS+HTTP streaming, ~$60/1M turbo $100/1M hd), OpenAI ✅. Speech-to-
speech: all three have a realtime API.

**Seam:** mirror `TranscriptionEngine` — `VoiceAgentSession` Protocol (`start`/`on_caller_audio`/
`stream_agent_audio`/`stop`) + `_AGENTS` registry {dummy, openai_realtime, diy_pipeline, vapi_sip} +
`get_voice_agent()` on `VOICE_AGENT_ENGINE`; TTS/STT/LLM sub-protocols so MiniMax-vs-OpenAI-TTS is a config switch
inside `diy_pipeline`; `dummy` keeps it offline-testable.

**Comparison:** DIY — latency ~0.8–1.5s, barge-in you-build (risk), cost components-only, effort highest, data
total on-box, no lock-in. OpenAI Realtime — ~0.3–0.5s, barge-in native, ~$0.06–0.11/$0.02–0.05, effort medium,
data high-locality (media transits OpenAI, control/recording on-box), single-vendor. Vapi — low latency + best
barge-in, ~$0.15–0.33, effort lowest, data low-locality (relay back), Vapi lock-in.

**Gaps:** OpenAI pricing/session-cap third-party; DIY latency estimated; MiniMax-no-STT inferred; MiniMax pricing
third-party; Vapi BYO-trunk inbound spec from Plivo/Twilio pages; slin↔µ-law bug community-reported — validate on
installed Asterisk during the external-media spike.

</details>

# AI-agent config + runtime model

Type: grilling
Status: resolved (2026-07-22 — grilling + domain-modeling; additive, no regression to existing pipeline)
Assignee: svillahermosa
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

## Answer (resolved 2026-07-22, via `/grilling` + `/domain-modeling`)

**One-line:** an AI agent is a **versioned library entity** (like a flow) whose immutable `config jsonb`
defines persona/voice/tools/knowledge/guardrails; at call time the ticket-06 `ai_agent` node spawns an
**asyncio session in the single-replica worker** that bridges Asterisk external-media ↔ the ticket-03
`VoiceAgentSession` engine (OpenAI Realtime default), and every terminal action, transcript, and outcome
flows back through the **existing** `call_events` → `calls` → analysis/GHL pipeline — **all additive.**

### Decision log (12 questions, all resolved with the recommended option)

**Config model**

1. **Agent = versioned envelope, not a mutable row.** `agents` + append-only `agent_versions`, mirroring
   the ticket-06 `flows`/`flow_versions` envelope. A call **pins `agent_version_id`** the moment the
   `ai_agent` node fires (exactly as it pins `flow_version_id` at `StasisStart` and `campaign_id` at
   ingest), so any past agent conversation is replayable with the exact prompt/voice/tools it used.
2. **Version body = a single `config jsonb` blob** (like `graph jsonb`), only identity/lifecycle promoted
   to columns (`agents.name`, `agents.active_version_id`, `agent_versions.version_number`, `created_by`,
   `created_at`). `config` fields for v1:
   - `system_prompt` — persona + instructions (the core)
   - `greeting` — opening line, or `null` to let the model open
   - `voice` — free string interpreted by the active engine (unknown → engine default)
   - `tools` — which of the curated catalog this agent may call (Q4)
   - `knowledge` — inline grounding text (Q5)
   - `knowledge_refs` — **reserved seam for document-backed retrieval, NOT implemented in v1** (Q5)
   - `guardrails` — `max_duration_s`, `max_turns`, `silence_timeout_s` (+ re-prompt retries) (Q12)
   - `model` tier — flagship vs mini (cost lever)
   Deliberately NOT in the blob: engine choice (Q3), the `agent_id`→flow binding (ticket 06/07),
   phone-number binding (ticket 07 — agents attach only via flow nodes, never to a number).
3. **Engine = one global `VOICE_AGENT_ENGINE` switch** (`dummy` default/offline, `openai_realtime` prod),
   behind the ticket-03 `VoiceAgentSession` Protocol+registry — the same shape as `TranscriptionEngine`.
   Every agent runs on the same engine; the agent config stays engine-agnostic so a version is portable.
   `voice` is the one engine-specific string, resolved by the active engine. **Per-agent engine = fog.**
4. **Curated 5-tool catalog, no arbitrary/webhook tools in v1.** Two kinds:
   - **Terminal tools → the node's ports** (the agent never knows phone numbers or flow structure; it
     calls a tool and the *flow* decides what's next): `transfer_to_human` → **`transfer`** port (flow
     wires to a `dial` node); `end_call`/`complete` → **`completed`** port; any agent/provider error or
     tripped hard-guardrail → **`failed`** port.
   - **In-conversation tools** (run and return, conversation continues): `capture_info` (structured
     name/reason/callback capture → recorded on the call); `lookup_caller` (read caller/attribution
     context on demand).
   **Custom/webhook tools + live GHL booking from the agent = fog** (the post-call GHL relay already
   fires from the completed call).
5. **Knowledge = hybrid, but only the cheap half is built.** `knowledge` inline text is implemented and
   injected into session context (part of the pinned version); `knowledge_refs` is reserved in the schema
   as the RAG seam but **not built in v1**. Full retrieval graduates from fog if a real corpus appears —
   no schema change needed then.

**Runtime**

6. **Agent session runs in the single-replica `worker` as an asyncio task per active agent leg** — the
   natural continuation of ticket 05 (ARI-WS consumer lives there) and ticket 06 (in-memory interpreter
   lives there). When the interpreter enters an `ai_agent` node it spawns a task that owns the
   external-media/AudioSocket leg + the engine's realtime WS, bridges audio both ways, and returns a
   **port result** to the interpreter. Both sides are async I/O and fit the existing event loop; `dummy`
   keeps it offline-testable. **Agent concurrency ceiling / process isolation = fog** (pairs with the
   already-fogged Asterisk HA/concurrency item; splitting to a separate media process later is a contained
   refactor behind the `VoiceAgentSession` seam).
   - *Transport:* inherit ticket 03's lean — **AudioSocket / external-media, G.711 µ-law** to avoid
     transcoding; read back `UNICASTRTP_LOCAL_ADDRESS/PORT` for return audio. Build detail, validated in
     the ticket-03 external-media spike; not re-decided here.
   - *Turn-taking/barge-in:* native OpenAI-Realtime server-VAD (ticket 03); not re-decided.
7. **`transfer` port = blind transfer** via the existing `dial` node: agent leg tears down, flow's `dial`
   bridges the caller to the target. Reuses ticket-06 machinery wholesale; the human still gets context
   via the call's `capture_info` + live transcript. **Warm transfer (spoken summary to the human first)
   = fog.**
8. **Entry context = minimal + lazy.** At node entry the interpreter injects only cheap always-known
   facts: `from`/`to`, `campaign_id`, **how the caller arrived** (e.g. "pressed 2 at main menu" /
   "after-hours overflow"), plus an optional per-node **`entry_context` hint** string the flow author
   writes on the `ai_agent` node. Richer caller history is pulled **on demand via `lookup_caller`** — no
   synchronous DB/attribution lookup at the latency-sensitive handoff moment (avoids dead air).

**Data**

9. **Realtime session transcript is authoritative for agent legs** — cheaper AND better. The engine
   already emits a turn-by-turn, speaker-attributed transcript (caller-side ASR + agent output text) for
   free; persist that turn list into the existing transcription store. The `VoiceAgentSession` seam
   **normalizes every engine's transcript into a common turn-list shape** (`dummy` returns a canned one).
   The `record` modifier (ticket 06) still captures a **mixed WAV for listen-back**, but it is **NOT
   re-transcribed** (that would pay for a second, worse, mono ASR pass). WAV transcription is the fallback
   only if no session transcript exists. This also delivers the stereo-transcription "who said what"
   payoff without needing dual-channel audio.
10. **`call_events` at milestones only** — `agent_started`, `agent_tool_called` (transfer / capture_info /
    lookup), `agent_ended` (with exit port + reason). Keeps events at ticket-06's node-transition
    granularity; the verbatim dialogue lives once in the transcript, not duplicated into the event stream.
11. **Reuse the existing projection — no dedicated agent tables.** Net-new storage is just: `calls.
    agent_version_id` (nullable, pinned when the node fires, mirrors `flow_version_id`) + `captured_info`
    and exit `outcome` (port + reason) as a small **jsonb** on the call record (or alongside the existing
    `call_analysis.tags` jsonb). The **existing post-call LLM analysis / attribution / job-lead / GHL
    relay pipeline runs unchanged** on the realtime transcript, so agent calls are first-class citizens of
    the same funnel, segregated only by `provider_id` (ticket 05/10). A full session reconstructs from
    `agent_version_id` (what it was) + `call_events` milestones (what happened) + transcript (what was
    said).

**Guardrails / failure fallback**

12. **Guardrails in config, all routed through the three existing ports** (no new flow-graph surface):
    - Graceful limit (`max_turns` / `max_duration_s` reached): signal the model to wrap up → exit
      **`completed`**; if it can't wrap in time, hard cutoff → **`failed`**.
    - Silence: re-prompt up to N times → caller-gone → **`completed`** → hangup.
    - Hard failure (Realtime WS drop, external-media setup fail, engine throws): **`failed`** → flow →
      voicemail. Ticket-06's flow-level `default_fallback` guarantees **no dead air** even if `failed`
      is unwired.
    - **Cost control: `max_duration_s` IS the cost lever** for v1 (duration bounds the dominant realtime
      cost). No real-time per-call cost metering; **per-day / per-agent budget caps = fog.** Still **log
      an estimated per-call cost** (from duration/tokens) so reporting can graduate later.
    - `max_duration_s` defaults well under ticket 03's reported ~30-min Realtime session cap.

### Net-new storage (all additive)
- **`agents`** — `id`, `name`, `active_version_id`, `created_at`.
- **`agent_versions`** — `id`, `agent_id`, `version_number`, `config jsonb`, `created_by`, `created_at`.
  **Immutable / append-only.**
- **`calls`** — gains nullable `agent_version_id` (pinned at node entry) + a `captured_info`/`outcome`
  jsonb (or reuse `call_analysis`'s jsonb).
- No changes to `call_events`, `transcriptions`, `call_analysis` *shapes* — agent data reuses them.

### The seam ticket 11 designs against (from ticket 06 / 03)
- Flow-graph node: `ai_agent` with `config.agent_id` + ports `completed` / `transfer` / `failed`.
- Engine: `VoiceAgentSession` Protocol — `start` / `on_caller_audio` / `stream_agent_audio` / `stop`,
  plus a normalized `transcript()` turn list and a terminal `port` result — registry `{dummy,
  openai_realtime, diy_pipeline, vapi_sip}`, selected by `VOICE_AGENT_ENGINE`.

### Downstream impact
- **Feeds ticket 10** (operator UX): the AI-Agents library form edits `config` fields (prompt/greeting/
  voice/tools/knowledge/guardrails), producing a new append-only `agent_version` on save — same
  edit-emits-a-version pattern as the flow rule-form. Agent is picked from a dropdown inside an `ai_agent`
  flow node (already locked in ticket 10), never bound to a number.
- **Feeds ticket 05** (data model): agent milestones are `call_events`; `agent_version_id` pin + jsonb
  reuse the projection; realtime transcript reuses `transcriptions`.
- **Feeds ticket 06** (flow graph): confirms the `ai_agent` node contract (`agent_id` ref + three ports +
  `record` modifier + per-node `entry_context` hint) — no schema change to ticket 06 needed.
- **Feeds ticket 13** (in-platform WebRTC): the blind-`transfer` → `dial` path is the same handoff a
  human operator's softphone will be a target of; no conflict.

### Fog graduated / recorded (schema/seam supports, not built in v1)
Per-agent engine selection; custom/webhook tools + live GHL booking from the agent; RAG over
`knowledge_refs`; warm transfer (spoken summary to the human); agent concurrency ceiling / separate media
process; per-agent & per-day cost budget caps (v1 has only `max_duration_s` + logged estimate).
**Outbound AI dialing** and **STT/TTS/LLM provider tuning** remain in the map's existing fog (not sharp
enough for tickets yet).

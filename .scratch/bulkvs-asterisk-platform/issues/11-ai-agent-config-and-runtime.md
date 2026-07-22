# AI-agent config + runtime model

Type: grilling
Status: closed (2026-07-22 — grilling + /domain-modeling; additive AI-agent module, no regression to Twilio/SignalWire/GHL paths)
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

## Answer
 (resolved 2026-07-22, via `/grilling` + `/domain-modeling`)

**Model:** an AI agent is a **first-class, versioned, reusable object** dropped into a flow's
`ai_agent` node (ticket 06). It runs as an on-box `VoiceAgentSession` in the single-replica `worker`,
bridging Asterisk AudioSocket ↔ OpenAI Realtime (ticket 03 default), and hands control back to the
ticket-06 interpreter through the node's `completed`/`transfer`/`failed` ports. Every seam mirrors an
existing pattern (`TranscriptionEngine`, `flows`/`flow_versions`, the analysis+GHL pipeline), so the
whole module is **additive** — the live Twilio/SignalWire/GHL paths are untouched.

### Decision log (14 questions, all resolved with the recommended option)

**CONFIG — what an agent is**
1. **Storage — versioned `agents`/`agent_versions` envelope**, mirroring `flows`/`flow_versions`.
   `ai_agent` node holds `{ agent_id }`; the flow pins `flow_version_id` at `StasisStart` (freezing the
   `agent_id` ref), and the **`agent_version_id` resolves when the call enters the node**. Editing an
   agent never mutates in-flight calls or past audit trails. Version body (`jsonb`): `persona`, `voice`,
   `greeting`, `model`, `tools[]`, `knowledge`, `guardrails`, `temperature`; row carries `engine`.
2. **Entry context — ambient + per-node vars.** Runtime always injects call facts
   (`caller_number`, `dialed_number`, business-hours state, `campaign`, prior DTMF digits); the node adds
   an authored `context` map + optional `greeting`/`objective` override. Persona uses `{{placeholders}}`,
   so one "receptionist" agent is parameterized per placement instead of cloned.
3. **Tools — fixed built-in registry, per-agent toggles.** No arbitrary LLM-issued HTTP in v1.
   *Flow-exit* (runtime intercepts, ends session, takes the matching port): `transfer(reason)` →
   **`transfer`**, `end_call(reason)` → **`completed`**. *In-call* (runtime runs, returns to model):
   `capture_lead(fields)` (→ analysis/GHL, see 11), `send_sms(body)` (→ ticket-08 `message_send` job from
   the dialed number). Any unhandled error → **`failed`**. The three ports are exactly the ways the model
   can leave the node.
4. **Knowledge — in-context blob.** A `knowledge` markdown field appended to the Realtime session
   instructions at handoff (single-org scale fits the context window). No RAG; `lookup_knowledge` is
   dropped from v1 (facts are already in-context).

**RUNTIME — how it runs**
5. **Host + transport — in `worker`, AudioSocket.** The interpreter spawns a `VoiceAgentSession`
   asyncio task (one per active agent call) alongside the ARI consumer/interpreter (tickets 05/09,
   `ASTERISK_ENABLED`-gated). Asterisk **AudioSocket/TCP G.711** ↔ worker ↔ OpenAI Realtime WS ↔ inject
   into the call bridge. No new service; separate media process is the escape hatch (fog).
6. **Turn-taking — OpenAI server-VAD + eager flush.** Rely on Realtime's native VAD/turn-detection/
   interruption; on barge-in the worker **stops enqueuing agent frames, flushes its outbound buffer to
   Asterisk, and issues ARI stop-playback**. Jitter buffer kept ≤200 ms so a flush is ~instant.
   Per-agent `vad_sensitivity` knob. No custom VAD (ticket 03's riskiest DIY piece, avoided).
7. **Node exit — session returns `{ port, data }`; interpreter drives.** `transfer(reason)` →
   `{port:"transfer", …}`, `end_call`/hangup/natural end → `{port:"completed", outcome, captured}`,
   error/timeout → `{port:"failed", reason}`. **The agent never bridges the call itself** — on transfer
   it tears down (external-media + WS closed), the interpreter takes the `transfer` port, and its *wired
   next node* (usually `dial`) does the ARI call-control. All orchestration stays in the interpreter
   (consistent with 05/06); no persisted cursor.
8. **Transfer target — agent decides *whether*, graph decides *where*.** `transfer(reason)` carries no
   destination; the operator wires the single `transfer` port to a `dial` node (human/DID/ring-group) or
   another `ai_agent` node. The LLM never invents a phone number. Agent-chosen multi-destination routing
   = fog.

**SEAM**
9. **Provider seam — mirror `TranscriptionEngine`.** `VoiceAgentSession` Protocol
   (`start(context)`/`on_caller_audio(frames)`/`stream_agent_audio()`/`stop() → {port,data}`) + `_AGENTS`
   registry + `get_voice_agent(engine)` resolved from the **per-agent `engine`** field; global
   `VOICE_AGENT_ENGINE` = default-for-new-agents / offline kill-switch (force `dummy`). **v1 ships
   `dummy` + `openai_realtime`**; `diy_pipeline` (OpenAI STT+LLM → **MiniMax TTS**) and `vapi_sip` are
   registered-but-stubbed (fog), as are the sub-provider STT/LLM/TTS Protocols inside `diy_pipeline`.
   `dummy` returns a canned outcome and injects no real audio → flows stay testable offline.

**DATA**
10. **Transcript & events.** The session writes the Realtime input+output transcript **inline** to the
    existing `transcriptions` table — **speaker-labeled (caller/agent) and segmented**, like the current
    dual-channel "who said what" shape — so **agent legs skip post-call STT entirely**. The `record`
    modifier records the bridge WAV through the existing recordings pipeline (05/06), unchanged.
    `call_events` stay at **node granularity** (one on entering the node, one on exit-via-port carrying
    outcome/reason); individual conversational turns are **not** events (they live in the transcript).
11. **Analysis & attribution — reuse the pipeline; capture is authoritative.** Attribution is unchanged
    (`campaign_id` via `to_number`, ticket 05). The **existing post-call analysis runs on the agent
    transcript** → same `call_analysis`/tags/summary/GHL relay, so agent calls appear in analytics
    identically to Twilio calls. `capture_lead` structured fields are stored **authoritative** on
    `call_analysis` (a `captured` jsonb) and are **not** overwritten by the classifier, which fills tags/
    summary around them. Zero downstream rework (honors the map's HARD CONSTRAINT).

**GUARDRAILS / FAILURE**
12. **Guardrails — time/silence caps + model tier.** Per-agent `max_call_seconds` (default ~300 s →
    graceful end via `completed`/`transfer`; also dodges the reported ~30-min provider session cap),
    `max_silence_seconds` (one re-prompt then end), `model` tier (`gpt-realtime` vs `mini`) as the cost
    dial. **No mid-call dollar meter in v1** (time ≈ cost); an aggregate monthly budget alarm = fog
    (pairs with ticket 09's deferred telephony alerting).
13. **Failure — lean on the `failed` port + `default_fallback`.** Connect-fail at handoff, WS drop,
    hard timeout, or non-graceful breach → `{port:"failed", reason}` → interpreter's `failed` port →
    unwired falls to the flow-level `default_fallback` (voicemail); **the caller never hits dead air**
    (ticket 06's guarantee already covers agent failure). **One bounded WS-reconnect retry** on a
    transient drop before declaring failure; the partial inline transcript is persisted; the exit event
    records `reason`.

**GRADUATED**
14. **Outbound AI dialing → new ticket 15**, blocked by 11 (it reuses this agent config + session seam).
    Distinct, statable questions live there: campaign list management, dial pacing, **TCPA/consent
    compliance**, answering-machine detection, and agent↔outbound-campaign binding. Everything designed
    above is **inbound** (agent answering a call at an `ai_agent` node).

### New tables / columns (all additive)
- **`agents`** — `id`, `name`, `engine`, `active_version_id`, `created_at`.
- **`agent_versions`** — `id`, `agent_id`, `version_number`, `config jsonb` (persona/voice/greeting/model/
  tools/knowledge/guardrails/temperature), `created_by`, `created_at`. **Immutable / append-only.**
- **`calls`** — stamps `agent_version_id` when a call enters an `ai_agent` node (nullable; only agent calls).
- **`call_analysis`** — add a `captured jsonb` for agent-captured structured lead data (authoritative).
- **config** — `VOICE_AGENT_ENGINE` (default/kill-switch), reuse `OPENAI_API_KEY`; agent-level `engine`
  selects the impl.

### Downstream impact
- **Consumes ticket 06** — the `ai_agent` node + its `completed`/`transfer`/`failed` ports and `agent_id`
  ref are exactly the seam designed against; `transfer` wires to a `dial`/`ai_agent` node.
- **Consumes ticket 03** — OpenAI Realtime over external-media (AudioSocket), pluggable seam realized.
- **Feeds ticket 05** — node enter/exit `call_events`; transcript reuses `transcriptions`; recording
  reuses the pipeline; analysis/attribution reuse `call_analysis`+GHL — all additive.
- **Feeds ticket 10** — the agent-config UI (library + node dropdown, "never bound to a number") is
  ticket 10's surface; this ticket defines what that UI edits.
- **Feeds ticket 13** — the operator softphone's "transfer to AI agent" target lands the call at an
  `ai_agent` node handled by this runtime.
- **Blocks new ticket 15** — Outbound AI dialing / campaign dialing.

### Fog graduated / recorded (schema/seam supports, not built in v1)
Custom webhook tools (LLM-driven outbound HTTP); RAG + `lookup_knowledge` knowledge store; separate media
process if worker CPU-bound; agent-chosen multi-destination transfer (named sub-ports); `diy_pipeline`
(MiniMax TTS) + `vapi_sip` engines and their STT/LLM/TTS sub-protocols; per-agent provider/latency tuning
+ barge-in budgets; aggregate monthly cost-budget alarm.

# AI Voice Agents — agreed design

> Outcome of a design interview (2026-09-03). Every decision below was explicitly chosen,
> not assumed. Where a decision has a non-obvious rationale it is recorded, because in this
> subsystem the obvious alternative is usually the one that dead-airs a caller or bills you
> twice.
>
> Companion: [`CRM_CONTEXT_SPEC.md`](CRM_CONTEXT_SPEC.md) — giving an agent the caller's
> current state from a CRM at call start (designed 2026-09-04, not yet built).
>
> Companion docs: [`CODE_MAP.md`](CODE_MAP.md) (how OWEN works today — predates the
> BulkVS/Asterisk half), [`FLOW_BUILDER_SPEC.md`](FLOW_BUILDER_SPEC.md) (the flow runtime
> these agents run inside), [`../asterisk/README.md`](../asterisk/README.md).

## The question this answers

> "I want an army of AI agents I personalise by voice and prompt, that can answer calls and
> make calls, redirect to a human or another flow, and save what they learn as data — not
> buried in a transcript. It must scale, and other projects should be able to use it."

## Scope — INBOUND ONLY (decided 2026-09-03)

**Agents answer calls. Agents do not place calls.** Agent-initiated outbound is designed in
[D8](#d8--outbound-agent-calls-run-through-a-flow--deferred) and deliberately **not built**.

Why the line is drawn here: inbound AI is legally unremarkable — a caller who dials you has
initiated contact, and answering with an AI is not a TCPA event. Outbound AI is a robocall
under the FCC's 2024 ruling that AI voices are "artificial", carrying $500–1,500 per call
with a private right of action and no cap. The engineering to do it safely (hard calling-hour
blocks, a voice suppression list, enforced AI disclosure, a consent ledger) is real work that
buys nothing until there is a reason to dial out.

**"Inbound only" does not mean no outbound legs.** An inbound call transferred to an external
number ([D9](#d9--transfer-targets-come-from-a-per-agent-allowlist)) still originates an
outbound leg over the trunk — that is a *forwarded* call, initiated by the consumer, and
raises none of the above. It does still double-bill; see D9.

## What exists today (verified in code, 2026-09-03)

The scaffolding is largely built. The audio is not.

| Piece | Status |
|---|---|
| `agents` + `agent_versions` — append-only versioned config (persona/voice/greeting/model/engine/tools/knowledge/guardrails) | ✅ Built |
| `calls.agent_version_id` pinned on node entry | ✅ Built |
| `ai_agent` flow node, ports `{default, transfer, complete, failed}` | ✅ Built |
| Engine registry + `VOICE_AGENT_ENGINE` kill-switch (`agents/session.py`) | ✅ Built |
| Closed tool registry — `transfer`, `end_call`, `capture_lead`, `send_sms` | ✅ Built |
| Guardrails, retry contract, transcript assembler, drive loop (unit-tested with fakes) | ✅ Built |
| `/agents` UI + `api/agents.py` | ✅ Built |
| **Audio transport** | ❌ **`openai_realtime.py:529` — `audiosock = None`, "placeholder wire-up point". `next_event()` raises `NotImplementedError`. No `externalMedia`, no AudioSocket anywhere in the repo or `asterisk/`.** |
| **Captured-data persistence** | ❌ **`interpreter.py:680` — `port, _data = await self.run_agent(node)`. Discarded. Four files describe `data["captured"]` feeding "the existing analysis `captured` path"; that path does not exist — no column, no key, nothing in the analysis engines.** |
| Outbound agent calls | ❌ `handle_outbound_call` is operator-softphone only |
| Custom / per-agent tools | ❌ Closed registry of four, code change per capability |
| `send_sms` agent tool actually sending | ❌ `sms_sender` seam never injected by `runtime.run_agent` |

**Host capacity (measured 2026-09-03):** 6 vCPU, 11 GiB RAM, load average 0.26 (~4%),
9.5 GiB available, 169 GB free disk, Asterisk 22.10.1. Ample headroom; no hardware purchase
is implied by anything below.

---

## D1 — Cascaded pipeline, not speech-to-speech

**Decision:** STT → LLM → TTS as separate, swappable stages.

**Rationale:** the requirement is BYO-LLM, specifically Chinese labs (DeepSeek, Kimi K2,
MiniMax). In speech-to-speech the model *is* the pipeline — OpenAI Realtime has no brain
slot, and none of those labs ship a Realtime-equivalent audio API. "BYO LLM" and "S2S"
cannot both be true. This reverses the direction `openai_realtime.py` was built in.

OWEN already runs MiniMax in production for call analysis over OpenAI-compatible function
calling (`analysis/classification.py:235`, `MINIMAX_BASE_URL`). DeepSeek, Kimi and MiniMax
are all OpenAI-compatible, so swapping the brain is a `base_url` + model name — a pattern
this repo has already proven.

**`openai_realtime.py` is refactored, not deleted.** ~400 of its 593 lines are
engine-agnostic and already unit-tested: `parse_guardrails`, `guardrail_port`,
`should_retry`, `dispatch_tool`, `TranscriptAssembler`, and the `_drive` loop with its
normalized `EV_SPEECH` / `EV_TOOL_CALL` / `EV_TICK` / `EV_ERROR` vocabulary. Extract those
into a shared base the cascaded engine inherits. Only `_OpenAIRealtimeConnection` — the
class that was never implemented — dies.

---

## D2 — A separate `owen-voice` container

**Decision:** the audio pipeline runs in its own container in the same compose stack,
separable-but-co-located: no `localhost` assumptions, credentials over the wire, a
configurable Asterisk host:port. It runs on the same box today; moving it later is config.

**Rationale:** the worker is capped at `cpus: '0.5', memory: 256M`
(`docker-compose.prod.yml:70-72`) — correctly sized for draining a queue and holding a WS,
not for concurrent audio pipelines. Raising it puts agent audio in CPU contention with
`recording_fetch`, `transcribe`, the mail poller, the billing reconcile **and the ARI
consumer every live call depends on**. An audio bug would take down call ingestion
platform-wide. Wrong blast radius.

Adopting jambonz or LiveKit (the option a greenfield project would take) was rejected: OWEN
already has a working SIP layer — flows, WebRTC softphone, recordings, CDR reconcile, bridge
control — and a second SIP brain means re-litigating decisions already debugged on live
calls.

The "no hardcoded host" discipline is not theoretical: `172.19.0.1` hardcoded in three
places broke during the August 2026 VPS migration.

---

## D3 — Audio leaves Asterisk via ARI `externalMedia` with AudioSocket encapsulation

**Decision:** `POST /channels/externalMedia` with `encapsulation=audiosocket,
transport=tcp`. The external-media channel joins the call's bridge like any other channel.

| | Dialplan `AudioSocket` | `externalMedia` RTP | **externalMedia + AudioSocket** |
|---|---|---|---|
| Transport | TCP | UDP — you own jitter/loss/reordering | TCP |
| Framing work | Trivial | RTP parser + jitter buffer | Trivial |
| Control model | Dialplan | ARI | **ARI** |
| Touches `extensions.conf`? | **Yes** | No | **No** |

**Rationale:** it is the only option with no trade-off. It stays inside the control model
`asterisk/README.md` declares non-negotiable ("ALL bridge/hold/blind-transfer go through the
BACKEND over ARI") and reuses the same `add_to_bridge` primitive as `ring_and_bridge` and
`dial_number`, so recording, transfer and hold keep working unchanged. And it requires **no
Asterisk config change** — relevant because that runbook already documents `pjsip.conf` as
unsafe to blind-rsync and a bare `envsubst` silently blanking `${EXTEN}` in a deployed
dialplan.

**Fallback if unsupported on the pinned build:** dialplan `AudioSocket` (a one-line dialplan
change), **not** RTP. Hand-rolling a jitter buffer is the larger risk.

**Codec:** AudioSocket delivers 8 kHz 16-bit signed linear mono. Feed that to STT directly —
Deepgram, AssemblyAI and Cartesia all accept slin16@8k, so there is no transcode in the hot
path. (The dead S2S config asked for `g711_ulaw` and was giving this away.)

---

## D4 — Human take-over, with call ownership enforced centrally

**Decision:** a supervisor can silently **listen** to an agent call (ARI snoop,
`spy=both, whisper=none`) and **take over** (operator softphone joins the bridge, the
external-media channel is ejected). Whisper/coach is dropped — you cannot voice-coach an LLM
mid-turn.

Take-over sets **call ownership** in a per-linkedid registry, modelled on the pattern
`flows/dtmf.py` already uses for DTMF and channel-event correlation. The agent session
observes it and returns a new terminal port `taken_over`, which the interpreter treats as
*return immediately, touch nothing*.

**The invariant, enforced inside `AsteriskAriClient` itself, not at each call site:**

> Once a call is human-owned, no automated path may hang up, play, record or bridge that
> channel again.

**Rationale — this is a live bug, not a hypothetical.** `_h_ai_agent` blocks for the whole
conversation holding `channel_id` = the caller's entry channel. `ai_agent` is not in
`TERMINAL_TYPES`, so when the session ends the interpreter resolves the port
(`interpreter.py:365-391`) → unwired → `default_fallback` (documented as "usually
voicemail") → **plays a voicemail greeting at a caller mid-sentence with a human operator**;
or with no fallback, `_safe_hangup()` → `ari.hangup(self.channel_id)` → **hangs up on the
caller the operator just rescued**.

This is the same failure class that already bit this codebase twice: outbound legs receiving
the voicemail greeting (`asterisk_consumer.py:120-122`), and the `play`/`dial` race that
bridged over a mid-sentence consent notice. Same root cause — a control path unaware another
actor owns the channel. Making it structural rather than remembered is the point.

Task cancellation was rejected: `interpreter.run()` has a `finally` that emits
`flow.call.summary`, and `_run_graph` scatters best-effort hangups. Cancelling mid-node
races your own instrumentation.

## D5 — Scope of take-over

- **`taken_over` is internal control, not a wireable graph port.** The graph vocabulary stays
  `{default, transfer, complete, failed}`. Every added port costs validator + editor + docs
  changes and invites an operator to wire something after a human call, which is nearly
  always wrong. Anything legitimately wanted afterwards already runs through the post-call
  pipeline (transcribe → analyze → relay).
- **Ownership is generic; ejection is agent-specific.** The registry and the ARI guard are
  call-level and cost nothing to generalise. The teardown differs per case (eject
  external-media / cancel a ringing leg / stop a recording), so only the agent case is built
  now. Intercepting a voicemail in progress ("I'm here, don't leave a message") is noted as a
  natural follow-on.

---

## D6 — Per-agent HTTP tools, declared in the version config

**Decision:** the closed registry stays for **platform** actions (`transfer`, `end_call`,
`capture_lead`, `send_sms`). Agents may additionally declare **custom HTTP tools** in their
version config: name, description, JSON-schema parameters, method, URL, headers, auth
reference, and `mode: sync | async`.

**This satisfies `tools.py:3` rather than violating it.** The property being protected is
*the LLM cannot choose an arbitrary URL* — and it still cannot. The URL set is fixed in an
immutable, validated, version-pinned config written by an operator. The model chooses *which
declared tool*, never *where*. The allowlist moves from Python source to versioned data.

It lands on machinery that already exists: `agent_versions.config` (immutable, append-only),
`validate_agent_config` (gates activation, not saving), `calls.agent_version_id` (answers
"which tools did this call have?"), and `flows/runtime._http_request` (HTTP with a hard
ceiling that never raises).

**MCP rejected for now:** runtime tool discovery breaks version pinning — you cannot
reconstruct what capabilities a past call had, which is the guarantee the whole versioning
design exists to provide. It also adds a proxy hop of latency and puts untrusted
server-authored tool descriptions into the model's context.

### Tool latency discipline

`_REQUEST_TIMEOUT_S = 5.0` is correct for a **flow** node (the caller is between prompts).
It is catastrophic **in-call** — five seconds of silence mid-conversation reads as a dropped
call.

| Mode | Budget | Use for |
|---|---|---|
| `sync` | **~800 ms hard**, with a filler phrase | Fast reads only |
| `async` | Returns `{queued: true}` instantly; completes on the `jobs` queue | Writes, slow lookups, third-party calls |

**Writes are async by default.** A lead being saved must never make a caller wait, and the
existing queue already guarantees delivery with backoff and dead-lettering.

**Direction:** agent → outward now. Other projects → OWEN (OWEN as a multi-tenant agent
platform) is deferred — it raises whose-numbers / whose-billing / whose-transcripts questions
that should not be answered before one agent has held one conversation.

---

## D7 — `call_captures` — structured data, first class

**Decision:** a new append-only table, **one row per capture event** (an agent may capture
twice in a call — name early, problem details later — and both matter with their timestamps).

```
call_captures
  call_id            FK → calls              (indexed)
  agent_version_id   FK → agent_versions     which agent config produced this
  capture_type       str                     the declared schema name
  fields             JSONB                   the structured payload
  captured_at        timestamptz
  relayed_to_ghl     bool                    existing relay-once guard pattern
  relayed_at         timestamptz
```

**Field vocabulary: shared core + per-agent extras.** A small controlled vocabulary every
agent uses for common things (`name`, `phone`, `address`, `intent`, `urgency`) plus free-form
`extra`. Same shape as `call_analysis` today, which pairs a controlled `category` enum with
free-form `tags` — a split that has already worked here. It keeps "how many emergency roof
leaks did the army capture this month" answerable across every agent while letting a
specialist record something nobody else needs.

**Why not the three tempting existing homes:**

| | Why it breaks |
|---|---|
| `call_analysis.tags` | `call_analysis` is **unique per call** and owned by the `analyze` job. An agent writing during the call and `analyze` upserting after it is a write race on one row. Also loses which agent version captured it. |
| `callers.label / company / role` | `label` is documented as **"manual override"**. The platform holds the line that humans win over models (`coalesce(override, model_value)`) everywhere else. |
| `contact_notes` | Free text, human-facing. Useless for querying. |

**Rule:** an agent capture never overwrites a human-entered field. Promoting a capture onto a
`caller` is an operator action or a separately-decided rule.

Captured data rides the existing `call_relay_ghl` handler (which already has the relay-once
guard and defer-while-analysis-pending logic) rather than needing a new relay path.

---

## D8 — Outbound agent calls run through a flow — **DEFERRED**

> **NOT IN SCOPE.** Retained in full because the analysis is expensive to re-derive and the
> conclusions do not expire. If agent-initiated outbound is ever picked up, this is the
> design — including the compliance guardrails, which are the reason it was deferred rather
> than merely postponed. Nothing below is built.

**Decision:** originate the callee; **its channel id is the Linkedid**; on answer, run a flow
graph whose first nodes are consent/hours and whose `ai_agent` node holds the conversation.
One real leg plus one media leg.

**Rationale:** outbound inherits the entire safety net instead of re-implementing it badly —
the consent `play` node with the blocking `play_and_wait`, `hours` evaluation, the `transfer`
port, `default_fallback` so a failed agent never dead-airs, the D4 ownership guard,
`flow.node.exit` / `flow.call.summary` events, and `flow_version_id` pinning.

`run_outbound_call` is the template: watch-both-legs-before-originating (no StasisStart
race), pre-bridge consent, recording, and a `finally` that always tears down both legs. An
agent call is that skeleton with the first leg swapped for an external-media channel.

**Wrinkle to design around:** `_h_entry` calls `ari.answer(channel_id)`, meaningless on an
outbound leg — the far end answers. The entry handler checks direction and skips `answer`.
Smaller blast radius than a new node type, and precisely the "an outbound leg entered a path
built for inbound" bug already seen in `asterisk_consumer.py:120-122`.

**Trigger: one at a time**, operator- or API-initiated, enqueued as an `outbound_agent_call`
job. Campaign dialing is deferred — it needs pacing, retry policy, answering-machine
detection, abandonment tracking, per-list consent provenance and a mass kill switch. The
queue already has `run_after`, so a dialer is later a scheduler on top of what exists.

### Compliance asymmetry — this is the sharpest constraint in the project

Inbound AI calls generally need no prior consent. **US outbound commercial AI calls do** —
the FCC treats AI voices as artificial/prerecorded, requiring prior express written consent,
disclosure at call start, and opt-out. Statutory damages are **$500–1,500 per call, no cap**.
EU AI Act Art. 50 (in force since 2 Aug 2026) additionally requires spoken disclosure at
first interaction, naming the operating company.

**Architectural consequence:** guardrails that are correctly **soft** for a human operator
must be **hard** for an agent. `telephony/outbound.py` is explicit today that its checks are
advisory — "SOFT + NON-BLOCKING", "There is NO hard DNC / calling-hours block", warnings "the
operator may ignore". That is right for a human exercising judgment and wrong for an
autonomous system that can repeat the same mistake 500 times before anyone notices.

For agent-initiated outbound:

- **Calling window — hard block**, not a warning string.
- **Opt-out — hard block.** `sms_opt_outs` generalises to a channel-aware suppression list.
- **Disclosure — activation-time error.** `validate_agent_config` must make it an *error*,
  not a warning, for an outbound-capable agent to lack AI disclosure in its greeting.
- **Consent record required before dial** — a record of *why* this number was callable.

---

## D9 — Transfer targets come from a per-agent allowlist

**Decision:** the agent chooses **which named destination**, declared in its version config.
It never supplies a number.

| Kind | Mechanism | Exists? |
|---|---|---|
| `number` | External PSTN over the trunk | ✅ `dial_number` |
| `operator` | A human's browser softphone | ✅ `dial_operator` |
| `flow` | Another flow, **internally, same channel** | ⚠️ new |
| `agent` | Hand off to another agent, same media bridge | ⚠️ new |

**Rationale:** identical to D6 — the LLM picks which declared thing, never where. An LLM that
can dial arbitrary numbers over the BulkVS trunk is a **toll-fraud primitive**: the attack is
a phone call, where someone spends two minutes being persuasive and gets the agent to
transfer to a premium-rate or international number. Prompt engineering is not a control here;
the allowlist is.

**`flow` and `agent` transfers stay internal — no PSTN round trip.** `services/billing.py:18-25`
records from live BulkVS records that "an inbound call that a flow forwards back out over the
same trunk bills as inbound minutes **AND** outbound minutes, seconds apart." Dialing your own
DID to reach its flow would double-bill every transfer, add PSTN latency and burn two trunk
channels for one conversation. Resolve the target DID to its assigned flow and run that graph
on the same channel — `_resolve_active_flow_version` already takes a dialed number and returns
a runnable graph.

The `agent` kind is the same trick one level down and is what makes this an army rather than
ten independent agents: a receptionist routing to specialists.

---

## D10 — Concurrency: reserved inbound capacity, fast-fail

**Decision:** **4 concurrent agent sessions**, all inbound. Enforced in `owen-voice` (the
service owning the resource). No slot available → **refuse immediately**, never queue.

> With outbound deferred (see Scope) there is nothing to reserve capacity *against*, so the
> inbound/outbound split is dropped and the limit is a single counter. The reservation design
> — 4 total with outbound capped at 1 — is what to reinstate if D8 is ever built; it is the
> answer to "an outbound call must never starve an inbound one", and preemption is the wrong
> alternative because dropping a live outbound call to free a slot is worse than sending an
> inbound caller to voicemail.

**Why fast-fail is the whole trick:** the existing design already handles exhaustion
correctly. No slot → the agent session returns `failed` → `_run_graph` routes to
`default_fallback` → voicemail or ring-operators. Capacity exhaustion is just another agent
failure, and the correct response was built and tested long ago. But if a slot request
*blocks*, the caller hears dead air while the code politely waits its turn — the exact
failure the architecture exists to prevent.

**The trunk ceiling is 10.** `GET /trunkGroups` reports `MaxIn: "10"` for trunk group
`vps-main-trunk` (verified 2026-09-03) — ten concurrent **inbound** channels, carrier-side.
`MaxOut` is null, so outbound legs (including transfers) are uncapped.

Four agent sessions out of ten therefore leaves **six channels always available** for
non-agent traffic — flow forwards, operator softphone, voicemail. Agents structurally cannot
consume the trunk. Note that the 11th simultaneous inbound call is rejected **by BulkVS**,
not by OWEN, so trunk exhaustion will not appear in application logs.

**The number is otherwise conservative and not a hardware limit** — the box has ~5.7 idle
cores. The first month's real risk is a runaway cost or a vendor rate-limit cascade, not
insufficient capacity. Raise once per-call cost data exists (D14).

**`EV_TICK` must survive the refactor.** `guardrail_port` is only evaluated when an event
arrives, so `max_call_seconds` / `max_silence_seconds` never fire in true dead air unless
something synthesises a tick on timeout — described in `openai_realtime.py:474-476`, in the
part that was never implemented. **A hung session holding a slot forever is how a 4-slot pool
becomes a 0-slot pool.**

---

## D11 — Provider slots

**Recommended stack:**

| Slot | Pick | Why |
|---|---|---|
| STT | **Deepgram Flux** | Integrated end-of-turn detection removes a VAD component and saves 200–600 ms — more perceived quality per dollar than any voice upgrade |
| LLM | Chinese model, **Western-hosted** | See below |
| TTS | **Deepgram Aura-2** or **Cartesia Sonic** | Aura-2 is trained on real call-centre audio → best pronunciation of **names, addresses and numbers**, which is the relevant property for a dispatch business capturing service addresses. Cartesia wins on TTFB (40–90 ms) |

Deepgram for both STT and TTS means one vendor, one credential, one latency profile — and
leaves their Voice Agent API as a fallback if the pipeline is ever collapsed.

**The LLM latency trap.** The VPS is European. DeepSeek, Moonshot/Kimi and MiniMax's own
endpoints are China-hosted: ~200–250 ms round-trip **per turn** against a total budget of
800 ms–1.2 s. DeepSeek and Kimi K2 are hosted by Together, Fireworks, Groq and OpenRouter on
US/EU infrastructure, and all are OpenAI-compatible — **use the model, not necessarily the
lab's own endpoint.**

### Config structure: profile + per-agent override, snapshotted at activation

Full per-agent customisation — prompt, LLM, voice, all independently switchable. Named
profiles exist as an **authoring-time** convenience.

**The version row stores the fully-resolved provider set.** If an agent merely *referenced* a
mutable profile, `calls.agent_version_id` would stop telling you what actually ran — edit the
profile and every past call's attribution silently becomes a lie. Snapshotting preserves the
guarantee the whole versioning model exists for, and it keeps full customisation
diagnosable: "is agent #7 bad because of its prompt or its model?" stays a SQL query grouping
outcomes by resolved model across the army.

**API keys never go in `agent_versions.config`** — that JSONB is immutable, readable, and
pinned forever. Store a key *reference* (`llm_key: "deepseek_together"`) resolved against
settings at runtime. Rotating a key must not require a new agent version.

---

## D12 — Agent slots

**Decision:** the `ai_agent` node references a **named slot** (`"receptionist"`); an
`agent_slots` table maps slot → agent. Swapping which agent answers is a data edit, not a new
flow version.

Note first that *changing an agent's prompt, voice or LLM already needs no flow change* —
author a new agent version and activate it; `Agent.active_version_id` is a mutable pointer by
design. Slots solve the different case: DID X should now be answered by **agent B** instead of
**agent A**.

**Rationale:** flow-version history keeps meaning "the routing changed" rather than "someone
tried a different agent"; **pinning is unaffected** because the concrete `agent_version_id` is
still resolved and pinned per call; and A/B testing becomes structurally possible — point a
slot at agent A for a week, agent B the next, and compare `call_captures` and analysis
honestly. It is the third application of a pattern already trusted twice
(`Flow.active_version_id`, `Agent.active_version_id`): a mutable pointer in front of immutable
content.

Number-level agent assignment bypassing the flow was rejected — it creates a second way a
call gets answered, and only one of them has consent playback, hours, the transfer allowlist,
ownership and `default_fallback`.

---

## D13 — The `owen-voice` ↔ OWEN contract

**Control: a blocking call.** OWEN's worker POSTs `owen-voice /sessions`; the request stays
open for the conversation and returns `{port, data}`. This maps 1:1 onto the existing
`RunAgentFn` signature — **zero interpreter changes** — and its failure mode is already
correct: if `owen-voice` restarts mid-call the request fails, `_h_ai_agent` catches it,
returns `failed`, and the flow routes to `default_fallback`.

**Side effects flow independently.** Captures and transcript are written to OWEN **as they
happen**, not returned at the end. A call that dies at minute four must still have the lead
captured at minute two — the principle already stated in the dead engine: "even a partial
transcript from a failed call is worth keeping."

**Custom HTTP tools execute in `owen-voice`, directly.** Latency decides it: routing every
tool call back through OWEN spends part of an 800 ms budget for no benefit, since the pinned
tool definitions are already in hand. Platform tools (`capture_lead`, `send_sms`, `transfer`)
go to OWEN because they touch OWEN's data.

**Writes get a new router: `/api/agent-runtime/*`, not an extension of `/api/ai/*`.**
`/api/ai` publishes its guarantee in four places including `AI_API.md`: *"All read-only —
nothing in this API can mutate platform data."* That is a contract machine consumers read to
teach themselves the API; bolting writes onto it would quietly falsify it. The new router
reuses the same `api_keys` table, `core/apikeys.py` hashing, scope gating, rate limiting and
`api_key_usage` audit, with its own `agent_write` scope. Same machinery, honest semantics.

That router is also the eventual seam for "other projects → OWEN" (D6), built now for one
consumer rather than speculatively for many.

---

## D14 — Per-call agent cost

**Decision:** extend `call_charges` with `ai.stt` / `ai.llm` / `ai.tts` kinds, carrying a
**derived** provenance marker distinct from carrier-**rated** rows.

**Rationale:** the question worth answering is "what did this call cost me, all in?" —
carrier minutes plus AI — which is one query only if they share a table, and `/billing`
already aggregates it. Reading vendor dashboards instead is a trap: the entire build-vs-buy
case is denominated in cost per minute, and without per-call data you cannot answer "did that
prompt change triple token usage?" or cash out D10's deferred "raise the limit once you have
data."

**The provenance marker is not pedantry.** `services/billing.py` opens with *"USAGE COST IS
NOT ESTIMATED"* because BulkVS publishes rated CDRs, and an earlier design that estimated
from Asterisk's CDR was found wrong in both directions. AI vendors issue no per-call charge —
cost is computed from usage they report against published rates. Conflating derived with
rated is exactly the mistake that module was rewritten to stop making. Keep the `unrated`
escape hatch for responses that omit usage: *"a bill that quietly under-reports is worse than
one that admits ignorance."*

**Build in from the start:**
- Cost recorded **per `agent_version_id`** — "version 7 doubled our token spend" is a query,
  free from pinning.
- **A spend cap, distinct from the kill switch.** `VOICE_AGENT_ENGINE` forces every agent to
  `dummy` — a *behaviour* switch. A daily/monthly ceiling that refuses new sessions
  (returning `failed` → fallback → voicemail, the same safe path as capacity exhaustion) is a
  *cost* switch. A runaway loop at 3 a.m. is what a retry queue and a 4-slot pool will not
  catch on their own.

---

## D15 — Build order

The only thing here that can invalidate the design is whether audio round-trips. Everything
else is code this codebase has demonstrably written before.

> **STATUS 2026-09-04.** All 8 steps built, deployed and green on the VPS at revision
> `c3e6a9d1f725`. Nothing has yet carried a real customer call — a test DID still needs
> pointing at an agent-bearing flow, which is the one remaining step and is data, not code.

## Deploy record — steps 6-8 (DONE 2026-09-04)

Deployed from `0e2c25f`. Both migrations applied cleanly on first run; `app` was brought up
alone to migrate while `worker` kept serving on the old image, then `worker` and `owen-voice`
followed. Verified afterwards:

| Check | Result |
|---|---|
| Alembic | `c3e6a9d1f725` (head) |
| New schema | `agent_slots`, `call_charges.provenance/agent_version_id/usage`, `call_captures` all live |
| Existing data | 31,661 calls · 8,212 events · 116 active numbers untouched |
| Existing billing | all 460 rows stamped `rated`, sum unchanged at $0.812290, 0 derived |
| Container tests | 12/12 pass, including the pre-existing telephony and billing suites |
| Public surface | api 200 · frontend 200 · `/api/ai` 401 · `/api/agent-runtime` 401 |
| ARI consumer | reconnected on `app=owen` |

Rollback point was `2194ddb` / `a1c4e7f2b830`; not needed.

**Migrations to apply** (self-apply at startup behind the advisory lock):

| Revision | Adds |
|---|---|
| `a1c4e7f2b830` | `call_captures` *(already deployed with step 5)* |
| `b2d5f8a3c914` | `agent_slots` |
| `c3e6a9d1f725` | `call_charges.provenance` / `.agent_version_id` / `.usage` |

**New env (all optional, all default to off/safe):**

```
AI_DAILY_SPEND_CAP_USD=0     # 0 = no cap. A COST switch, not the behaviour kill-switch.
```

**Verify after deploy** — these need the container and could not run locally:

```bash
docker exec callmon_app alembic current           # expect c3e6a9d1f725 (head)
docker exec callmon_app python -m tests.test_ai_cost
docker exec callmon_app python -m tests.test_transfer_allowlist
docker exec callmon_app python -m tests.test_ownership
docker exec callmon_app python -c "from app.main import app;   print(sorted(r.path for r in app.routes if 'agent-runtime' in r.path))"
```

**Rates ship as published list prices.** Check `app/services/ai_cost.py::DEFAULT_RATES`
against a real OpenAI invoice before planning around any figure it produces. Every row it
writes is stamped `derived`, never `rated`, precisely so it cannot be mistaken for one.

| # | Ships | Risk retired |
|---|---|---|
| ✅ **0** | **Verify** — `curl` ARI for `encapsulation=audiosocket` on 22.10.1; confirm the BulkVS trunk channel limit | Both open unknowns. Hours. |
| ✅ **1** | **Echo spike** — `owen-voice` accepts AudioSocket and echoes audio back. Call a test DID, hear yourself. No LLM/STT/TTS. | **The entire transport**, bidirectionally, at 8 kHz, through a real bridge |
| ✅ **2** | Cascaded pipeline, one hardcoded agent: Flux → LLM → TTS, with barge-in | Latency budget, turn-taking, vendor integration |
| ✅ **3** | Wire to the `ai_agent` node on a **test DID only**. Guardrails + `EV_TICK` carryover. Fast-fail concurrency (4). | Flow-runtime integration |
| 🚦 | **GATE — no production DID before step 4** | |
| ✅ **4** | Take-over: ownership registry, `taken_over`, snoop-listen, barge, central ARI guard | "The agent is broken and a human must grab this call" |
| ✅ **5** | `call_captures` + inline transcript + fix the `_data` discard | The data requirement |
| ✅ **6** | Transfer allowlist (4 kinds) + agent slots | The army becomes routable |
| ✅ **7** | Custom HTTP tools + `/api/agent-runtime` | Integration surface |
| ✅ **8** | Cost rows + spend cap | Economics |

**Why the gate sits there:** step 3 is the first moment a real caller can reach an agent. The
stated requirement is that a human can seize a call when the agent misbehaves — so production
traffic before step 4 leaves that scenario with no answer. Test DIDs before, real DIDs after.

Agent-initiated outbound (D8) was formerly step 8 and is now out of scope entirely; if
reinstated it belongs after everything above, because it reuses what inbound proves — flow
execution, agent session, transfer, capture, take-over — and building it earlier means
building it twice.

---

## Step 1 result — VERIFIED both directions, 2026-09-04

`POST /spike/loopback` on the live host, with Asterisk as both audio source and sink.

**RECEIVE** (`mode=echo`, a stock sound played into the bridge):

```
connected: true   rx_frames: 246   rx_bytes: 78720   peak_amplitude: 25648
duration: 5.06s   verdict: "OK — audio flowed both ways"
```

246 frames × 320 bytes = 78,720 bytes exactly, and 246 × 20 ms = 4.92 s against a 5.06 s
window — real-time pacing with no drift.

**SEND** (`mode=tone`, a 440 Hz sine into an otherwise SILENT bridge, bridge recorded):

```
tx_frames: 241   rx_frames: 0        <- a bridge whose only member is the media channel
                                        has no audio source; this is why the tone runs on
                                        its own clock rather than being rx-driven
recording: 8000 Hz mono 16-bit, 4.76 s
  peak 8000/32767   rms 5656   dominant frequency 440.0 Hz
```

`peak` equals `tone.DEFAULT_AMPLITUDE` exactly, `440.0 Hz` equals `tone.DEFAULT_HZ` exactly,
and 5656 is 8000/√2 — the RMS of a pure sine at that amplitude. The recording is bit-level
consistent with what owen-voice generated, so it can only have come from bytes written back
down the socket.

**Therefore:** ARI `externalMedia` with `encapsulation=audiosocket, transport=tcp` works on
this build; UUID correlation works; framing is correct; `slin` 8 kHz is carried intact; and
audio moves in both directions at real-time cadence. **D3 is proven and the design's one
load-bearing assumption holds.**

Still worth doing once: a **real inbound call**. The loopback exercises no trunk, no codec
negotiation with BulkVS and no network RTP, so it proves the AudioSocket transport rather
than the whole call path.

## Step 2 result — pipeline working end to end, 2026-09-04

`POST /spike/loopback {"mode":"agent"}` — the cascaded pipeline against Asterisk's own speech,
no phone, no caller.

```
turns: 1        vad_starts: 8   vad_ends: 8    rx_frames: 1514   tx_frames: 180
transcript:
  caller: "and hang up commands to simulate the actions of a standard telephone."
  agent:  "I'm here to help with your roofing needs! May I have your name, please?"
```

The caller line is a real sentence from the `demo-congrats` prompt, so STT transcribed audio
that genuinely crossed the bridge; the reply is in character from `AGENT_SYSTEM_PROMPT`; and
180 frames (3.6 s) of synthesized speech went back. **STT → LLM → TTS all work against the
existing OpenAI credentials.**

### The latency number, and what it costs

**`last_turn_ms: 3354`** — roughly 3× the spec's 800 ms–1.2 s target (§14). Not a defect;
it is the price of the substitution this build made deliberately:

| Cause | Cost | Fix |
|---|---|---|
| Batch STT — one Whisper request *after* the turn ends | the whole utterance's upload + inference, serialised after the caller stops | **Deepgram Flux**: streams during speech and detects end-of-turn itself (§4) |
| Local VAD hangover — 700 ms of silence before we even believe the turn ended | 700 ms, every turn | Same: Flux's EOT replaces `dsp.TurnDetector` entirely |
| TTS synthesized whole before playout starts | first-audio waits for the last word | Stream TTS and start playing the first chunk |

So the single highest-value upgrade is **Deepgram for STT**, which removes the first two rows
at once and lets `app/dsp.py`'s turn detector be deleted rather than tuned. That is exactly
what §4 predicted, now measured on this system rather than taken on faith.

### Known artifact of the self-test, not of the pipeline

`max_quiet_run` showed the longest silence in the test stream was **26 frames (520 ms)**
against the 700 ms end-of-turn threshold, so no turn could ever end until the threshold was
overridden for the test. That is the audio *source*: `demo-congrats` is continuous recorded
speech whose inter-sentence gaps never reach the pause a human leaves. 700 ms remains the
default for real callers; `/spike/loopback` takes `vad_end_frames` to override it.

**Still outstanding: a real inbound call.** Neither loopback exercises the trunk, BulkVS codec
negotiation, or network RTP — and no human has yet heard the agent speak.

## Delivered so far

| Step | Evidence |
|---|---|
| 1 — transport | 246 frames received at exact real-time cadence, peak 25,648; a 440 Hz tone recorded back out of Asterisk at exactly the amplitude and frequency owen-voice generated |
| 2 — pipeline | Real phone call, 3 turns, English conversation. Latency tuned from 2986 ms to ~1700 ms time-to-first-audio |
| 3 — flow wiring | `owen_voice` engine registered; `POST /sessions` blocks and returns a port; capacity refuses instantly at 4; a dead channel correctly reports `failed` |
| 4 — take-over | 19 checks, incl. proof that a seized call plays no voicemail and is not hung up on, with a control test proving ordinary flows still fall back |
| 5 — captures | `call_captures` migrated and live; the interpreter no longer discards agent output; tool calling collects the lead |

### Latency, measured on a live call

| Stage | First build | Now |
|---|---|---|
| STT | 1295 ms (whisper-1) | **452–583 ms** (`gpt-4o-mini-transcribe`) |
| LLM | 567 ms | unchanged — the cheapest stage |
| TTS | 1112–1453 ms | streamed, sentence-pipelined |
| **Time to first audio** | **2986 ms** | **~1700 ms** |

Further gains need vendors: Deepgram Flux removes most of STT *and* the 600 ms turn-detection
hangover (and makes `dsp.TurnDetector` deletable); Cartesia cuts TTS first-byte from ~629 ms to
under 100 ms.

### Audio faults found and fixed on live calls

| Symptom | Cause |
|---|---|
| Caller heard fragments | The agent's own voice through a speakerphone tripped barge-in → half duplex |
| Replies in Chinese/Arabic | STT hallucinates fluent text from noise; the LLM followed → English pinned, non-Latin transcripts dropped |
| 20 ms of audio per reply | `np.frombuffer` on odd-length HTTP chunks |
| Metallic | 3-tap box resampler passed 4 kHz at −3.5 dB → 63-tap FIR at −43 dB |
| Robotic/choppy | Streamed chunks queued straight to playout left gaps inside words → whole-sentence buffering |

## Verification items

1. ✅ **AudioSocket support on the pinned Asterisk 22.10.1** — cleared 2026-09-03.
   `app_audiosocket.so`, `chan_audiosocket.so` and `res_audiosocket.so` are all loaded and
   Running (support level: extended), alongside `res_ari_channels.so` (core).
   `chan_audiosocket.so` is what `externalMedia` needs for `encapsulation=audiosocket`;
   `app_audiosocket.so` means the D3 dialplan fallback is available too.
2. ✅ **BulkVS trunk concurrent-channel limit** — cleared 2026-09-03. `GET /trunkGroups`
   (same Basic-auth REST creds as `/tnRecord`) reports `MaxIn: "10"`, `MaxOut: null` for
   `vps-main-trunk`. See D10. *Worth adding to `bulkvs_client.py` — the account's own
   capacity is currently invisible to OWEN, and the full endpoint list from `/openapi` is:
   accountDetail, campaigns, e911Record, exchanges, ipHost, mdr, messageSend, orderTn,
   portTn, tnRecord, trunkGroups, twilio, validateAddress, validatePortability, voice,
   webHooks.*
3. ⬜ **Vendor accounts + concurrency limits** (STT/TTS, LLM host). Needed at step 2, not
   step 1. At 6 idle cores these will bind long before hardware does.

## Invariants

Non-negotiable properties this design must preserve. Each traces to a bug this codebase has
already paid for.

1. **Never dead air.** Every failure path — agent error, capacity exhaustion, spend cap,
   `owen-voice` restart, transport drop — resolves to `failed` → `default_fallback`.
2. **Once a call is human-owned, no automated path touches that channel** (D4), enforced
   inside the ARI client.
3. **The LLM chooses which declared thing, never where** — tools (D6) and transfer targets
   (D9).
4. **Version pinning stays truthful.** Providers snapshotted at activation (D11), concrete
   `agent_version_id` pinned per call regardless of slot indirection (D12).
5. **Humans win over models.** Agent captures never overwrite human-entered fields (D7).
6. **`/api/ai/*` stays read-only** (D13).
7. **Derived cost is never presented as rated cost** (D14).
8. **Agents answer; agents do not dial.** Nothing in the agent runtime may originate a call to
   a number the platform was not already talking to. The moment that changes, D8's hard
   guardrails are a prerequisite, not a follow-up.

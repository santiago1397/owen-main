# Voice stack migration — Deepgram Flux + Aura-2

> Decisions agreed 2026-09-09. Vendor survey and pricing behind them:
> [`VOICE_VENDOR_RESEARCH.md`](VOICE_VENDOR_RESEARCH.md). The agent design this modifies is
> [`AI_AGENT_SPEC.md`](AI_AGENT_SPEC.md) (D1 cascaded, D11 provider slots, D13 the owen-voice
> contract, D14 cost); caller context is [`CRM_CONTEXT_SPEC.md`](CRM_CONTEXT_SPEC.md).

## The question this answers

The cascaded pipeline works and is measured at **~1700 ms** time-to-first-audio against a
target of 800 ms–1.2 s (AI_AGENT_SPEC §14). Two thirds of that is structural, not vendor
slowness: a local energy VAD waits 600 ms to believe a turn ended, and only then does a batch
STT round trip begin. This document records the decision to move both legs to Deepgram, what
that buys, what it costs, and the order it lands in.

**Nothing here is measured yet.** Every latency figure is the vendor's own claim applied to
our own measured baseline. It is written down so that when the numbers come in, the gap is
visible rather than forgotten.

---

## M1 — Deepgram Flux for STT, and why the seam has to change

`flux-general-en` over `wss://api.deepgram.com/v2/listen`, `encoding=linear16`,
`sample_rate=8000` — the AudioSocket format exactly, so caller bytes reach Deepgram with no
resampling ([Flux quickstart](https://developers.deepgram.com/docs/flux/quickstart)).

Flux is not a faster `transcribe()`. **It inverts the control flow**, and that is the whole
point:

```
today   VAD decides the turn ended  ->  we call transcribe(audio)  ->  text
Flux    we stream audio continuously ->  Flux tells us the turn ended, with the text
```

The existing `SpeechToText` protocol (`transcribe(pcm8k) -> str`) cannot express that, so
Flux gets its own seam (`StreamingSpeechToText`) rather than being forced through the old
one. The batch protocol and `OpenAISTT` stay exactly as they are — see M6.

Two savings, and they are different things:

| Saved | How |
|---|---|
| **600 ms** | `VAD_END_FRAMES=30` hangover disappears. Flux decides end-of-turn semantically |
| **452–583 ms** | The STT round trip disappears. The transcript is *already final* when `EndOfTurn` fires |

## M2 — Eager end-of-turn is ON

`eager_eot_threshold` makes Flux emit `EagerEndOfTurn` on a *predicted* turn end, then either
`TurnResumed` (the caller kept talking — discard) or `EndOfTurn` (committed).

The protocol is explicit about what may be done with a draft
([eager EOT guide](https://developers.deepgram.com/docs/flux/voice-agent-eager-eot)):

- `EagerEndOfTurn` → **draft the LLM reply only. Never start TTS.**
- `TurnResumed` → discard the draft entirely.
- `EndOfTurn` → *now* speak. Deepgram guarantees the `EndOfTurn` transcript **exactly matches**
  the `EagerEndOfTurn` transcript, so the draft needs no reconciliation.

So eager mode hides the **LLM** leg (~400–570 ms) behind the caller's last words. It does not
hide TTS, and speculative playback is explicitly warned against — a `TurnResumed` after audio
has started is an agent talking over a caller who never stopped.

**Cost:** 50–70% more LLM calls, because drafts get thrown away. At $0.0013/conversation-hour
this is not a cost decision. It was taken for latency alone.

## M3 — Deepgram Aura-2 for TTS

`aura-2-thalia-en` by default, `encoding=linear16`, `sample_rate=8000` over
`wss://api.deepgram.com/v1/speak`
([Speak streaming reference](https://developers.deepgram.com/reference/text-to-speech/speak-streaming)).

Native 8 kHz is the real win, ahead of the ~430 ms of claimed TTFB improvement: **it deletes
`Downsampler24to8` and `resample.Decimator` from the hot path.** Every OpenAI TTS byte is
currently resampled 24 kHz → 8 kHz in-process, and that code path has already produced one
audible fault (a 3-tap box filter passing 4 kHz at −3.5 dB, heard as metallic; fixed with a
63-tap FIR). Removing a resampler removes the whole bug class, and it removes CPU from a box
that must never starve Asterisk.

Aura-2 was chosen over Flux TTS (80 ms claimed, $0.045/1k vs $0.030) because it is trained on
call-centre audio, and this agent reads back **service addresses and phone numbers**. Flux TTS
is one config value away — `VOICE_TTS_MODEL` — and both should be bench-run in the same
loopback pass.

## M4 — Layered configuration: env default, per-agent override

Exactly the pattern `VOICE_AGENT_ENGINE` already uses:

1. A **global kill-switch** wins when set — flip everything back to OpenAI without touching
   agent data.
2. Otherwise the **agent version's config** decides (`stt_provider`, `tts_provider`,
   `tts_model`, `voice`, …), snapshotted immutably at activation like every other agent field.
3. Otherwise the **env default**.

This is D11's "provider slots" cashed out. The override keys ship in this change but stay
unset, so behaviour is env-driven until someone deliberately pins an agent.

## M5 — Voice identifiers will break, and the failure must be soft

`Agents.tsx:34` hardcodes 11 **OpenAI** voice names. Aura-2's are entirely different
(`aura-2-thalia-en`, `aura-2-apollo-en`, … 49 English voices). Every existing agent's stored
`voice` string becomes meaningless the moment the provider changes.

**An unknown voice resolves to the provider default and raises an activation *warning*, never
an error.** Activation failing on agents that were fine yesterday is a worse outcome than an
agent speaking in the wrong voice for one call. The UI voice list becomes a function of the
selected TTS provider.

## M6 — OpenAI stays, and there is no automatic failover

The OpenAI STT/TTS classes remain registered and working. Deepgram becomes the default.

**No mid-call vendor failover.** It sounds like resilience and is actually a bug: switching
vendors mid-conversation changes the voice mid-sentence. A Deepgram outage takes the path the
system already has and already tests — the session returns `failed`, and the flow interpreter
routes to `default_fallback` (voicemail). A caller never hears dead air; they hear voicemail,
which is a designed outcome rather than an improvised one.

## M7 — What does NOT change in this migration

Deliberate omissions, each of which would otherwise look like a free win:

| Not changing | Why |
|---|---|
| `PRIME_FRAMES` (400 ms) | Worth 200 ms, and the least defensible on the list. The code comment records that 400 ms of priming *still underran twice in a two-turn call*. Underrun is an audible gap mid-word — a bug class already paid for. Measure Aura-2's chunk cadence first |
| `HALF_DUPLEX=true` | Keep ignoring inbound audio while the agent speaks — and that means **Flux is not fed those frames either**. It has to be that way: while we are speaking, inbound audio is our own echo, and feeding it to Flux would let the agent end its own turn. The consequence is that half duplex still costs barge-in, exactly as it does today; Flux's `StartOfTurn` becomes the barge-in signal only once half duplex is turned off. **Do not change this and the STT vendor in the same step** — a regression would be unattributable |
| `dsp.TurnDetector` | Stays in the tree behind the provider switch until Flux's p90 tail has been heard on real calls. It is the fallback, and deleting it is a separate commit with evidence attached |
| The LLM leg | $0.0013/conversation-hour, ~0.2% of the bill. D11's Chinese-model reasoning is a **latency** argument, not a cost one |

## M8 — The latency budget

Caller-perceived, from the caller's last syllable to the first agent audio frame:

| Stage | Now | Projected | Source of the saving |
|---|---|---|---|
| Turn detection | 600 ms | ~260 ms | Flux EOT replaces the energy hangover (**p50 claim**) |
| STT | 452–583 ms | 0 ms | Transcript is final when `EndOfTurn` fires |
| LLM → first sentence | ~400 ms | ~0 ms *(eager)* | Drafted during the caller's last words |
| TTS first byte | ~629 ms | ~200 ms | Aura-2 claimed <200 ms |
| Playout priming | 400 ms | 400 ms | Unchanged on purpose (M7) |
| **Time to first audio** | **~1700 ms** | **~860 ms** | |

Inside the 800 ms–1.2 s target — **if two vendor claims hold**. The one most likely to
disappoint is the first: 260 ms is Deepgram's **p50**, and their published **p90 is ~1 s,
p95 ~1.5 s**. Some turns will feel worse than today's fixed, predictable 600 ms. `eot_threshold`
is the knob; lowering it trades false turn-ends for latency.

## M9 — Cost, and the direction nobody expects

| Leg | Today | Recommended |
|---|---|---|
| STT | $0.090 (batch, utterances only) | **$0.462** (streaming, socket-open time) |
| LLM | $0.0031 | $0.0013 |
| TTS | *not derivable* | $0.900 |
| **Total** | **≥$0.093** | **$1.363/hr** |

**Streaming STT costs 5× more, not less.** Batch bills only the caller's utterances (~30 min
of a 60-minute call); a streaming socket bills the whole hour. This is the honest price of
deleting the turn detector — 840 ms of caller-perceived latency for ~$0.37/hr — and
`AI_DAILY_SPEND_CAP_USD` must be resized knowing it.

Two `services/ai_cost.py` changes follow, and both are defects that exist **today**,
independent of Deepgram:

- **`stt_audio_seconds` changes meaning.** `pipeline.py` accumulates `len(audio)/(8000*2)` —
  the sum of *utterance* durations. Correct for batch billing, **wrong by ~2× against any
  socket-billed vendor.** For Flux it must be the media session's wall-clock duration.
- **`tts.gpt-4o-mini-tts = 0.015` is wrong now.** That is the `tts-1` *per-character* rate
  applied to a *token-billed* model, and OpenAI publishes no character↔token conversion. Under
  D14's own rule — "a bill that quietly under-reports is worse than one that admits ignorance"
  — it should be an `unrated` row, which `ai_cost.py` already produces for free when the rate
  key is absent. Every TTS figure the Billing tab has shown is fiction.

## M10 — Rollout order

1. **Loopback bake-off.** `POST /spike/loopback` now takes `stt_provider` / `tts_provider` /
   `tts_voice` / `tts_model`, so two stacks can be compared against the same audio minutes
   apart — through the *same* code path a live call takes, not a parallel one that could pass
   while production fails. Ten minutes of this beats a week of vendor claims.

   ```bash
   # on the VPS, over the ssh session (the control API is loopback-only)
   BASE=http://127.0.0.1:8099/spike/loopback
   curl -s $BASE -H 'content-type: application/json' \
     -d '{"mode":"agent","seconds":25,"stt_provider":"openai","tts_provider":"openai"}' \
     | python -c 'import json,sys; d=json.load(sys.stdin); print("openai  ", d["last_first_audio_ms"], d["last_turn_ms"])'
   curl -s $BASE -H 'content-type: application/json' \
     -d '{"mode":"agent","seconds":25,"stt_provider":"deepgram","tts_provider":"deepgram"}' \
     | python -c 'import json,sys; d=json.load(sys.stdin); print("deepgram", d["last_first_audio_ms"], d["last_turn_ms"])'
   ```

   Watch `eager_hits` vs `eager_retracted` in the same snapshot: a retraction rate that is not
   comfortably below the hit rate means eager mode is burning LLM calls without buying latency,
   and `VOICE_DG_EAGER_EOT_THRESHOLD` should go up (or to 0).

   **The loopback's known blind spot, unchanged since step 2:** `demo-congrats` is continuous
   recorded speech whose inter-sentence gaps never reach the pause a human leaves. It could not
   end a turn under the old 700 ms threshold without an override — and it is exactly the kind of
   audio Flux's semantic EOT should handle differently from an energy VAD. Treat a good loopback
   number as necessary, not sufficient.
2. **A throwaway DID** pointed at a test flow. Nothing customer-facing hears Deepgram until a
   human has heard it on a real phone over the real trunk — steps 1 and 2 of the agent build
   were booked exactly this way, and both found faults no loopback could have.
3. **Per-agent override** (M4) on one live agent.
4. **Env default** for everyone.

Deepgram ships **alone**, before any CRM tool work. It changes the audio path for every call
and has a measurable pass/fail; landing it clean means a later tool misbehaviour is not also an
audio suspect.

---

# Agent capability decisions

These were settled in the same session and are recorded here because they change what the
agent config means. They are **not** part of the Deepgram change and ship after it.

## M11 — One agent, and a capped prompt

No routing between specialist agents, no RAG. One agent answers everything.

That makes the prompt the product, and it makes the cap load-bearing:

| Field | Budget | Holds |
|---|---|---|
| `persona` | ~1k chars | Who it is, and the hard rules |
| `knowledge` | **6k chars, enforced, with a UI counter** | The situation playbook |
| per-customer data | — | **Tools. Never the prompt.** |

`knowledge` is today **the one completely unbounded input in the loop** — nothing truncates it
in the UI, in `AgentVersionSave` (a bare `dict`), in `build_spec`, in `remote.py`, in
`AgentConfig`, or in `system_prompt`. It is concatenated whole into the system prompt by a
`@property` recomputed **every turn** (`pipeline.py:183-194`). A 50 KB blob is ~12k tokens
re-billed per turn, silently. The caller-context path solved this exact problem with
`MAX_SUMMARY_CHARS`/`MAX_FACT_CHARS`/`MAX_FACTS`; `knowledge` never got the same treatment.

**6k is a starting line, not a finding.** Hitting it is the signal that specialist agents
(D12 `agent_slots`, already built and unused) deserve revisiting — with real calls to argue it.

## M12 — Disclosure: confirm, never enumerate

The agent may **confirm** a fact the caller has already stated. It may **never enumerate**
matches.

> "I see a job at 14 Oak Street" ❌  ·  "Yes, that job is scheduled Thursday" ✅

Caller ID is trivially spoofable, so even a phone match is weak authentication. The failure
this prevents is concrete: a caller says "I'm calling about the Johnson job" and the agent
reads a stranger their customer's home address, in a recorded voice, on a line the business
owns.

## M13 — The guarantee lives in the tool contract, not the prompt

**A prompt instruction is not an enforcement mechanism.** A model told "do not list matches"
will list matches eventually.

`call_custom_tool` (`providers.py:315`) returns the CRM's parsed body **straight to the model,
unexamined** — there is no response shaping anywhere in the custom-tool path. So if an endpoint
returns an array, the model gets the array and M12 evaporates regardless of the prompt.

Since the CRM clone is ours and its logic is unwritten, the boundary is free to build:

```
GET  /agent/customer/by-phone?number=   -> {has_open_job: bool, job_summary: string|null}
POST /agent/customer/confirm            -> {confirmed: bool, detail: string|null}
     body: {name, stated_fact}
```

**Multiple matches return `{confirmed: false, reason: "ambiguous"}` — never the candidates.**
The model cannot enumerate because it is never handed an array. This mirrors
`flows/transfer.py`, where the anti-toll-fraud property is an allowlist rather than an
instruction: *the model chooses which declared thing, never what the thing is.*

A `max_items` collapse in `custom_tools.normalise()` is the defence-in-depth version, and stays
speculative until a second CRM exists.

## M14 — Tool credentials never enter versioned config

`custom_tools` declarations carry a `headers` dict stored in `agent_versions.config` JSONB —
**immutable, versioned forever, and readable through the agents API.** A CRM key placed there
is plaintext in the database, replicated across every version row, and rotating it means
re-versioning every agent.

`${ENV_VAR}` interpolation in tool headers, resolved in owen-voice at call time. The
declaration stores `{"Authorization": "Bearer ${CRM_API_KEY}"}`; the secret stays in
`.env.prod`.

## M15 — Both CRM integrations coexist, for now

The clone will become the single read source. Until then:

| System | Role |
|---|---|
| **GHL** | Caller context at call start (`context_provider`, the 1.2 s pre-greeting blob) + the existing lead-relay writes |
| **Clone** | Mid-call tool answers |

Two known defects in the GHL read path, unresolved and inherited:

- `agent_runtime.py:204` filters `status == "open"` — **closed jobs are fetched and discarded.**
- `agent_runtime.py:216` falls back to `pipelineStageId` when GHL omits `stageName`, which it
  often does, so **the agent can currently read a raw UUID aloud.**
- `fix_workiz.py:66-68` records that `/contacts/{id}/opportunities` "returns nothing useful
  here" — the exact endpoint `contact_opportunities` depends on. **Settle this before building
  anything else on that path**; if the endpoint is unreliable, no amount of tooling saves it.

---

## What shipped (2026-09-09)

Built, compiling, and covered by 51 new checks — but **not yet run against Deepgram or a
phone**. Everything below is verified logic, not verified behaviour.

| Area | Files |
|---|---|
| Flux streaming STT + its own `StreamingSpeechToText` seam | `owen-voice/app/providers.py` |
| Aura-2 TTS, native 8 kHz, no resampler | `owen-voice/app/providers.py` |
| Turn state machine + the playback gate | `owen-voice/app/pipeline.py` |
| Provider layering (lock > agent pin > env) | `providers.resolve_provider`, `config.py` |
| Per-agent pins on the wire | `backend/app/agents/remote.py`, `owen-voice/app/agent_api.py` |
| Knowledge cap + provider-aware voice validation | `backend/app/agents/service.py` |
| UI: provider selects, provider-aware voice list, character counter | `frontend/src/pages/Agents.tsx` |
| `${ENV_VAR}` tool credentials | `owen-voice/app/custom_tools.py` |
| Flux/Aura-2 rates; `gpt-4o-mini-tts` made `unrated` | `backend/app/services/ai_cost.py` |
| Closed jobs surfaced; stage UUID suppressed | `backend/app/api/agent_runtime.py` |
| Bake-off knobs on `POST /spike/loopback` | `owen-voice/app/main.py` |

Three decisions made while building, worth recording because they are not obvious from the
sections above:

1. **Flux could not reuse the `SpeechToText` seam.** `transcribe(pcm) -> str` assumes the
   caller decides when a turn ended. That is the exact assumption Flux inverts, so it got its
   own protocol and a separate registry — which is what stops `get_stt()` ever returning
   something the batch path cannot call.
2. **Eager mode gates PLAYBACK, not generation.** Rather than duplicating the turn machinery,
   a turn started on a prediction runs normally and blocks at one `asyncio.Event` before any
   audio is queued. On the local-VAD path that event is pre-set, so that path is untouched.
3. **`tts_model` in usage now comes from the engine.** For Deepgram the voice *is* the model
   (`aura-2-thalia-en`), so the old `model or settings.TTS_MODEL` would have attributed
   Deepgram spend to OpenAI. Each engine reports its own resolved id.

One test had to be **changed rather than fixed**: `test_ai_cost.py` asserted "nothing is
unrated when usage is complete" using `gpt-4o-mini-tts` as its fixture — it was encoding the
mispricing. The fixture moved to the shipping stack, and a new test now asserts the
token-billed model stays `unrated`, so the wrong rate cannot be helpfully added back.

## Open, and honestly unknown

| Unknown | Why it matters |
|---|---|
| Flux p90 tail on real 8 kHz calls | 260 ms is a p50. p90 ≈ 1 s would make some turns worse than today |
| **8 kHz telephony WER — for every vendor** | Unpublished industry-wide. It decides whether the agent hears "4127" correctly. The only number that matters, and nobody prints it |
| Aura-2 voice quality at 8 kHz | Vendor demos are 24 kHz studio audio. Judge down a real phone, never on laptop speakers |
| Whether 6k `knowledge` is enough | A guess. One agent must cover every situation (M11) |
| The clone's endpoints | Do not exist yet. M13 is a contract to build against |

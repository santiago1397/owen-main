# Voice vendors — STT and TTS for the agent pipeline

> **Retrieved 2026-09-09.** Every price and latency figure below carries a source URL and was
> read off the vendor's own pricing page, API reference, model card or first-party benchmark
> post on that date. Nothing is estimated. Where a vendor publishes no number, the cell says
> **not published** rather than carrying a plausible-looking figure.
>
> **What was verifiable:** current list pricing and billing units for every vendor; concurrency
> ceilings for most; 8 kHz/µ-law support from API references; end-of-turn parameters and their
> defaults; licences for open models.
>
> **What was NOT verifiable, and matters:** *every latency number in this document is
> vendor-published and vendor-measured.* Not one independent benchmark exists on any primary
> source consulted. Deepgram's "#1 on the Voice-Agent Quality Index" and AssemblyAI's "41%
> faster than Deepgram Nova-3" are each a vendor's own competitive claim about a rival. Also
> unverifiable: telephony (8 kHz narrowband) word error rate for almost every model — the
> number that decides quality here is the one nobody publishes.
>
> Companion: [`AI_AGENT_SPEC.md`](AI_AGENT_SPEC.md) — D1 (cascaded), D3 (AudioSocket at 8 kHz),
> D10 (concurrency), D11 (provider slots), D13 (the `owen-voice` contract), D14 (per-call cost).

## The question this answers

> "The pipeline works and time-to-first-audio is ~1700 ms against an 800 ms–1.2 s target.
> D11 named Deepgram Flux and Aura-2/Cartesia on faith, before anything was built. Now that
> there are measurements, which vendors actually earn the slots, what do they cost per hour,
> and can `dsp.TurnDetector` finally be deleted?"

Short answer: **yes, Flux earns its slot and the turn detector dies with it** — but the cost
model has a surprise in it that D14 could not have predicted, and it points the wrong way.
See [V6](#v6--cost-model-the-honest-version).

---

## Where the system is today (measured, not asserted)

From `AI_AGENT_SPEC.md` "Latency, measured on a live call" and the code as it stands:

| Stage | Current | Mechanism |
|---|---|---|
| Turn detection | **600 ms** | `dsp.TurnDetector`, energy VAD, `VAD_END_FRAMES=30` × 20 ms |
| STT | **452–583 ms** | `providers.OpenAISTT`, one batch POST per turn, `gpt-4o-mini-transcribe` |
| LLM to first sentence | ~400–567 ms | `OpenAICompatibleLLM.reply_stream`, `dsp.split_speakable` |
| TTS first sentence | ~629 ms | `OpenAITTS.synthesize_stream`, whole sentence buffered before playout |
| Playout priming | **400 ms** | `pipeline.PRIME_FRAMES = 20` |
| **Time to first audio** | **~1700 ms** | `session.last_first_audio_ms` |

Two things to hold onto, because they change the arithmetic later:

1. **The 600 ms hangover is not inside the 1700 ms.** `config.py` says so explicitly — "this
   silence is pure perceived latency on EVERY turn and is not counted in turn timings (the
   clock starts when the turn ends)". **What the caller actually experiences is ~2300 ms.**
2. **The stage times sum to more than 1700 ms** because sentence-level pipelining overlaps
   them. Do not read the table as an additive budget; read it as where the time lives.

---

## V1 — The evaluation columns, and why these ones

D11 treated end-of-turn detection as one bullet among several. The measurements say it is the
whole ballgame: turn detection (600 ms) plus batch STT (500 ms) is **1100 ms of the caller's
~2300 ms**, and a streaming STT with native end-of-turn collapses *both* into one number,
because the transcript is already final the moment the turn is declared over.

| Column | Why it is here |
|---|---|
| **Native end-of-turn** | Removes 600 ms hangover *and* the STT round trip. The single largest lever. |
| **Native 8 kHz** | D3 pinned slin16@8k with "no transcode in the hot path". A vendor forcing 16 kHz adds a resampler to a path that already has `resample.py` and a history of audio bugs. |
| **Concurrency on the entry tier** | `MAX_SESSIONS=4`, trunk `MaxIn=10`. A vendor capping a self-serve tier at 1–3 streams is disqualifying, not inconvenient. |
| **WebSocket streaming** | `pipeline.py` calls `synthesize_stream`; a batch-only vendor is a non-starter for the live leg. |
| **Billing unit** | D14 requires cost expressible in `services/ai_cost.py`. A vendor billing in credits or audio tokens with no published conversion cannot be rated — it becomes an `unrated` row, which is the honest outcome but a useless one. |

---

## V2 — STT comparison

All figures retrieved 2026-09-09. `$/hr` normalizes to one hour of **streaming connection**
unless noted; for batch STT you pay only for utterance audio, which is roughly half.

| Vendor / model | Price (vendor's unit) | **$/hr** | Native EOT | 8 kHz | Entry concurrency | Streaming | Self-host |
|---|---|---|---|---|---|---|---|
| **Deepgram Flux EN** | $0.0077/min regular; **$0.0065/min promo** ([pricing](https://deepgram.com/pricing)) | **$0.462** | **Yes** — `eot_threshold` 0.5–1.0 (def 0.7), `eager_eot_threshold`, `eot_timeout_ms` ([docs](https://developers.deepgram.com/docs/flux/configuration)) | **Yes** — `linear16`/`mulaw` + `sample_rate=8000` ([ref](https://developers.deepgram.com/reference/speech-to-text/listen-flux)) | **150 WSS on PAYG** | `wss://api.deepgram.com/v2/listen` | Enterprise; Ampere+ GPU; own node ([docs](https://developers.deepgram.com/docs/flux-self-hosted)) |
| Deepgram Nova-3 mono | $0.0077/min regular; $0.0048 promo | $0.462 | No | Yes | 150 WSS PAYG | v1 WSS | Enterprise |
| **AssemblyAI Universal-Streaming** | **$0.15/hr** ([pricing](https://www.assemblyai.com/pricing)) | **$0.15** | **Yes** — `end_of_turn_confidence_threshold` (def 0.4), `max_turn_silence` ([spec](https://www.assemblyai.com/docs/streaming/api-spec/streaming-websocket)) | **Yes** — `sample_rate` 8000–96000, `pcm_mulaw` | 100 new streams/min PAYG (concurrent "unlimited" per marketing — pages conflict) | `wss://streaming.assemblyai.com/v3/ws` | **Yes, same price as cloud**, air-gap OK, 48 streams/instance ([page](https://www.assemblyai.com/deployments/self-hosted)) |
| **Cartesia Ink-2** | 3 credits/sec audio; ~**$0.39/hr** derived on Scale ([pricing](https://docs.cartesia.ai/pricing)) | ~$0.39 | **Yes** — semantic endpointing, `turn.start` / `turn.eager_end` / `turn.end` ([blog](https://www.cartesia.ai/blog/ink-2)) | `pcm_mulaw` yes; **8000 Hz not confirmed in writing** | 8 free / 12 Pro / 20 Startup / **60 Scale** ([pricing](https://cartesia.ai/pricing)) | `wss://api.cartesia.ai/stt/websocket` | Not published |
| Cartesia Ink-Whisper | 1 credit/sec; **$0.13/hr** on Scale ([blog](https://www.cartesia.ai/blog/introducing-ink-speech-to-text)) | $0.13 | **No** — you send `finalize` | as above | as above | same | Not published |
| Speechmatics RT Enhanced | $0.43/hr ([pricing](https://www.speechmatics.com/pricing)) | $0.43 | **No** — `end_of_utterance_silence_trigger` is a silence timer, 0–2 s, **default disabled** | `mulaw` yes; explicit 8000 **not documented** | **2 free / 50 Pro** | `wss://eu.rt.speechmatics.com/v2/` | Enterprise only; **CPU-only container viable** (0.25 cores + 1.2 GB first session) |
| Speechmatics RT Standard | $0.24/hr | $0.24 | No | as above | as above | same | as above |
| Azure Speech real-time | $1.00/audio hour ([pricing](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/)) | **$1.00** | Not in the Speech SDK — `azure_semantic_vad` exists only in **Voice Live**, which accepts **16/24 kHz only** ([docs](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/voice-live-how-to)) | **Yes** in the SDK — PCM 8000, MULAW/ALAW in WAV | **F0: 1** / S0: 100 (shared with translation) | SDK only; raw WS protocol **not published** | Disconnected container, gated, **$74,100/yr entry** |
| Google STT V2 | $0.016/min ([blog, 2023-08-10](https://cloud.google.com/blog/products/ai-machine-learning/google-cloud-speech-to-text-v2-api)) — **current per-model table not retrievable** | $0.96 | No | **Yes** — MULAW @ 8000 accepted, "16000 is optimal" ([ref](https://docs.cloud.google.com/speech-to-text/v2/docs/reference/rest/v2/projects.locations.recognizers)) | 300 concurrent streams/region | gRPC bidi; **5-minute stream cap** | On-prem $0.024/min + Anthos |
| OpenAI `gpt-live-transcribe` | $0.017/min ([model](https://developers.openai.com/api/docs/models/gpt-live-transcribe)) | $1.02 | **Yes** — `semantic_vad` with `eagerness` ([docs](https://developers.openai.com/api/docs/guides/realtime-vad)) | **Yes** — `audio/pcmu`, `audio/pcma` | RPM/TPM only; **concurrency not published** | `wss://api.openai.com/v1/realtime?intent=transcription` | No |
| **OpenAI `gpt-4o-mini-transcribe`** *(incumbent)* | **$0.003/min** ([pricing](https://developers.openai.com/api/docs/pricing)) | $0.18 (stream) / **~$0.09 (batch, utterances only)** | No | Batch WAV — we upsample nothing, we wrap 8 kHz PCM | RPM/TPM only | file `stream=true`; **we use batch** | No |
| OpenAI `whisper-1` | $0.006/min | $0.36 | No | Batch | — | **No** — docs state whisper-1 does not support `stream=true` | No |
| Groq `whisper-large-v3-turbo` | **$0.04/hr** ([docs](https://console.groq.com/docs/speech-to-text)) | **$0.04** | No | Batch upload | 20 RPM, 7.2K audio-sec/hr free | **No — batch only, and a 10-second minimum bill per request** | No |

### What this table decides

**Four vendors do native end-of-turn: Deepgram Flux, AssemblyAI Universal-Streaming, Cartesia
Ink-2, and OpenAI's Realtime transcription intent.** Everything else leaves
`dsp.TurnDetector` alive and the 600 ms hangover with it. That is the sorting criterion.

Of those four:

- **Flux** is the only one where 8 kHz µ-law is *documented and demonstrated* — Deepgram's own
  telephony guide says "requesting mulaw at 8 kHz mono matches Twilio's Media Streams format
  exactly, so the caller's bytes flow to Deepgram with no resampling"
  ([docs](https://developers.deepgram.com/docs/twilio-and-deepgram-stt)), and their reference
  outbound-telephony agent uses Flux. It also has the most permissive self-serve concurrency
  in the entire survey: **150 concurrent WebSockets with no monthly commitment**, against a
  system that needs 4.
- **AssemblyAI** is **3× cheaper** and has real EOT confidence, but bills **socket-open time,
  not audio** — their own wording: "A WebSocket open for 60 minutes with 30 minutes of audio
  sent is billed for 60 minutes." On telephony with hold time that inflates past the headline.
  And its two own pages contradict each other on concurrency.
- **Cartesia Ink-2** is the most interesting late entrant — semantic endpointing described as
  "the model reads meaning, not silence" — but **8000 Hz is not confirmed in its API docs**,
  and Ink-Whisper's own published **phone-call WER of 0.19** is an order of magnitude worse
  than its other categories. Cartesia publishes no telephony WER for Ink-2 at all.
- **OpenAI Realtime transcription** is the incumbent-adjacent option and is `audio/pcmu`
  native, but at **$1.02/hr it is 2.2× Flux** for the same job, and OpenAI publishes no
  concurrent-session limit at all.

### Vendor-claimed latency, flagged as such

| Claim | Source | Self-serving? |
|---|---|---|
| Flux cuts agent response latency **200–600 ms** vs stitched STT+VAD; **P90 ≈ 1 s, P95 ≈ 1.5 s** EOT; eager EOT **150–250 ms earlier at 50–70% more LLM calls** | [Deepgram launch post](https://deepgram.com/learn/introducing-flux-conversational-speech-recognition) | Yes — including a "Voice-Agent Quality Index" Deepgram itself publishes |
| AssemblyAI **~300 ms immutable** transcripts; **41% faster median than Deepgram Nova-3** | [launch post](https://www.assemblyai.com/blog/introducing-universal-streaming) | Yes — explicitly a competitive benchmark against a named rival |
| Cartesia Ink-2 **88 ms**, 0.1 s time-to-final, **6.5% WER** vs claimed 9.2% Deepgram | [Ink-2 blog](https://www.cartesia.ai/blog/ink-2) | Yes — same pattern |
| Speechmatics "real-time latency <1 s" | [pricing](https://www.speechmatics.com/pricing) | Consistent with `max_delay` floor of 0.7 s; note the **default is 4.0 s** |

Three vendors each claim to beat the other two. Bench on real 8 kHz call audio before believing
any of them.

---

## V3 — Self-hosted STT on this box

The user asked whether STT specifically could run on the VPS. It is the one leg where that is
even arguable, so it gets a real answer rather than a wave.

**The constraint.** `AI_AGENT_SPEC.md` line 57 measured the host at **6 vCPU, 11 GiB RAM, no
GPU**. It is not dedicated: native Asterisk (dropped RTP is a dropped call), native Postgres,
and four containers (`app` 1 CPU/512M, `worker` 0.5/256M, `owen-voice` 2 CPU/1G, `frontend`
0.5/128M). A self-hosted STT process realistically gets **~2 cores and ~2 GB, CPU-only**, and
must serve **4 concurrent streams** — an aggregate RTF budget of roughly 0.25.

| Option | Genuine streaming? | Published CPU speed | RAM | 8 kHz telephony WER | Verdict |
|---|---|---|---|---|---|
| **faster-whisper** (CTranslate2 int8) | **No — batch.** README: "segments is a *generator* so the transcription only starts when you iterate over it" | small int8: **1m42s for 13 min audio on 8 threads of an i7-12700K** → RTF ≈ 0.13 *on eight desktop threads* ([README](https://github.com/SYSTRAN/faster-whisper)) | **1477 MB** for small int8 | **Not published** | ❌ Fails the first question. And 1477 MB of a 2 GB budget for one model. |
| **whisper.cpp** | **No** — `examples/stream` is self-described as "a naive example", re-transcribing a sliding window; "very basic VAD detector" | Published benches are **encoder-only**: small/8 threads/Ryzen 5950X = **1393 ms per 30 s window** ([issue #89](https://github.com/ggml-org/whisper.cpp/issues/89)) | tiny ~273 MB / base ~388 MB / small ~852 MB | **Not published at any size** | ❌ at `small`. `tiny`/`base` might fit the compute envelope but burn a full 30 s padded encoder pass per update, with zero published narrowband accuracy. |
| **Moonshine v2 Tiny Streaming** | **Yes — genuinely incremental.** "caches the input encoding and part of the decoder's state"; "any length of audio… no zero-padding required" ([docs](https://github.com/moonshine-ai/moonshine)) | **69 ms end-of-phrase latency on "Linux x86"** — CPU unnamed, thread count unpublished, and the maintainers warn that column "read[s] pessimistically". **CPU RTF: not published.** | 34 M params, int8 `.ort` | **Not published** | ⚠️ **The only serious candidate** — and unproven on exactly the two numbers that decide it. |
| **Vosk small-en** | **Yes** — `PartialResult()` per chunk, true Kaldi streaming | **Not published** for English | ~300 MB runtime (maintainer's figure) | **The only honest published telephony numbers in the survey — and they are bad:** `en-us-0.22` scores 5.69 on LibriSpeech but **29.78 on callcenter**; gigaspeech 30.17; aspire 33.82 ([models](https://alphacephei.com/vosk/models)) | ⚠️ Fits the box; **no English narrowband model exists**, and the model that fits has no telephony eval at all. |
| NVIDIA Parakeet TDT 0.6B | v2 offline | **RTFx 3380 — on a GPU at batch 128.** No CPU support statement, no CPU benchmark | ≥2 GB just to load | **6.32% on 8 kHz µ-law** — the best telephony number anywhere in this document ([card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2)) | ❌ **Needs a GPU.** Holds the receipts you cannot use. |
| NVIDIA Canary-1B-v2 | Offline | RTFx 749 on GPU | **≥6 GB to load** | Not published | ❌ Triple the entire RAM budget before inference. |

### The three findings that settle it

1. **Everything Whisper-based is batch wearing a streaming costume.** `ufal/whisper_streaming`,
   the reference LocalAgreement-2 implementation, publishes **3.3 seconds of latency** and gets
   there by re-transcribing overlapping windows. That is not a telephony turn budget.
2. **Nothing self-hostable does end-of-turn detection.** Confirmed across every model above. So
   **`dsp.TurnDetector` and its 600–700 ms hangover survive under every self-hosted option.**
   Self-hosting saves marginal money and forfeits the single largest latency win — which is the
   metric the whole exercise exists to move.
   - The nearest open alternative is **Pipecat `smart-turn-v2`** (BSD-2, semantic, audio-native,
     94.8 M params) — but its published CPU figure is **410 ms per inference on 16 cores**
     ([card](https://huggingface.co/pipecat-ai/smart-turn-v2)). On 2 cores × 4 streams that is
     not a rounding error, and no 8 kHz result is published.
   - **LiveKit's turn detector is licence-blocked**: "You cannot use the LiveKit models on a
     standalone basis or with any other frameworks."
   - **Silero VAD** is MIT, <1 ms per 30 ms chunk on one thread, and **natively 8 kHz** — a
     strictly better energy VAD than `dsp.rms_of`, but still acoustic, not semantic.
3. **Every model except Vosk requires 16 kHz input**, so PSTN audio must be upsampled 2×. The
   CPU cost is small; the accuracy cost is not, and nobody publishes it. Upsampling does not
   restore the 4–8 kHz band the PSTN discarded — you are feeding a 16 kHz-trained model a
   signal with the top half of its expected spectrum empty.

### Break-even, with the volume math

The VPS is already paid for, so marginal compute is ≈ $0. Cloud STT at Flux's regular rate is
**$0.462 per conversation-hour**. Savings therefore scale purely with agent hours:

| Agent conversation hours / month | Cloud STT cost (Flux) | What self-hosting saves |
|---|---|---|
| 50 | $23 | $23 |
| 200 | $92 | $92 |
| 704 — **the realistic ceiling**: `MAX_SESSIONS=4` fully saturated 8 h/day × 22 workdays | **$325** | $325 |
| 2,880 — theoretical max: 4 sessions × 24 h × 30 d | $1,331 | $1,331 |

**Verdict: do not self-host STT.** The realistic ceiling on this deployment is ~$325/month of
saving, and that is at *full saturation of every one of four slots for a full working day,
every working day*. Against that: one engineer-week to build it, a permanent CPU-contention
risk on the box that runs Asterisk (D2's entire argument for a separate container was blast
radius), an unmeasured narrowband WER, and — decisively — **the 600 ms hangover stays**. You
would spend real engineering to make the product slower in order to save less than the cost of
the VPS itself.

The one case that flips this: a GPU appears, and `parakeet-tdt-0.6b-v2` becomes runnable. Its
**6.32% WER on 8 kHz µ-law** is better than anything any cloud vendor publishes for telephony.
Revisit then, not before.

---

## V4 — TTS comparison

All cloud. Self-hosting TTS was **considered and rejected in one line**: the box has no GPU,
and the only Apache/MIT options with real streaming (Orpheus, 3.78 B params, 15 GB fp32) want
one; the CPU-viable ones (Kokoro) publish **no performance data at all** and pipeline only at
sentence granularity, which is what the current pipeline already does. Coqui XTTS-v2 is
additionally **licence-blocked** — CPML permits "only non-commercial use", Coqui is defunct
since 2024-01-03, and there is no one left to sell an exception.

Normalized to **$/1000 characters**, and to **$/hr** at the convention Inworld and Hume both
publish — *~1000 characters ≈ 1 minute of speech* — assuming the agent talks for **30 of every
60 minutes**, i.e. 30,000 characters/hour.

| Vendor / model | Price (vendor's unit) | **$/1k chars** | **$/hr** | Claimed TTFB | 8 kHz | Entry concurrency | WS | Cloning |
|---|---|---|---|---|---|---|---|---|
| **Deepgram Aura-2** | $0.030 / 1k chars PAYG; $0.027 Growth ([pricing](https://deepgram.com/pricing)) | **$0.030** | **$0.90** | **<200 ms**, RTF 0.111 ([post](https://deepgram.com/learn/introducing-aura-2-enterprise-text-to-speech)) | **Yes** — `mulaw`, `sample_rate` 8000 ([ref](https://developers.deepgram.com/reference/text-to-speech/speak-streaming)) | **45 WSS on PAYG**, 60 Growth | Yes | **None. No cloning of any kind.** |
| Deepgram Flux TTS | $0.045 / 1k chars | $0.045 | $1.35 | "as little as 80 ms" | same API | 45 free **until 2026-09-12** | Yes | None |
| **Rime Mist v3** | $0.03 / 1k chars ([pricing](https://www.rime.ai/pricing)) | **$0.030** | **$0.90** | **37 ms TTFA P50** — lowest published anywhere | **Yes** — `mulaw`, any `samplingRate` | **20 (Starter)** — but Rime's own pricing blog says 5, unresolved | `/ws3` | Enterprise/Growth only, 30–60 min audio, <7 business days |
| Rime Coda | $0.05 / 1k chars | $0.050 | $1.50 | 96 ms TTFA P50 | Yes | 20 | `/ws3` | as above |
| **Inworld TTS-2 Flash** | $15 / 1M chars On-Demand ([pricing](https://inworld.ai/pricing)) | **$0.015** | **$0.45** | **20 ms TTFB** (P90, server-side, network excluded — and Inworld's own two pages disagree P90 vs P99) | **Yes** — `MULAW` @ `sampleRateHertz: 8000`, µ-law *restricted* to 8 kHz | **5 On-Demand** / 10 Creator / 50 Builder / 150 Developer | Yes | Instant, from 3 s audio, all users |
| Inworld TTS-2 | $25 / 1M chars | $0.025 | $0.75 | 100 ms TTFB | Yes | as above | Yes | as above |
| **Cartesia Sonic-3.6** | ~1 credit/char; Scale $299/8M credits ([pricing](https://cartesia.ai/pricing)) | **$0.0374** (Scale) / $0.050 (Pro) | **$1.12** | "under 90 ms" ([blog](https://www.cartesia.ai/blog/sonic-3.6)) | **Yes** — `pcm_mulaw` @ 8000, their own Twilio recommendation | **2 free / 3 Pro / 5 Startup / 15 Scale** | Yes, `raw` container only | Instant (10–60 s); Pro 2 slots Startup, 4 Scale; **cost not published** |
| **ElevenLabs Flash v2.5** | $0.05 / 1k chars, flat at every tier ([API pricing](https://elevenlabs.io/pricing/api)) | **$0.050** | **$1.50** | **~75 ms**, footnoted "excluding application & network latency" ([models](https://elevenlabs.io/docs/models)) | **Yes** — `ulaw_8000`, `pcm_8000`, no tier restriction | 4 free / 6 Starter / 10 Creator / 20 Pro / **30 Scale** — Flash gets 2× the v2 limits | Yes | **IVC on all plans incl. Free**; PVC Creator+, identity-verified, **own voice only** |
| Azure Neural TTS | $15 / 1M chars ([pricing](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/speech-services/)) | **$0.015** | $0.45 | "<300 ms", no methodology | **Yes** — `raw-8khz-8bit-mono-mulaw` in the streaming set | F0 **20 req/60 s, not adjustable**; S0 30 TPS default | SDK WS; raw protocol not published | CNV: **$2,903/mo per endpoint** + "only customers managed by Microsoft" + biometric consent verification |
| Azure Neural, 4000M commit | $24,000/mo for 4 B chars | $0.006 | $0.18 | as above | Yes | as above | as above | as above |
| Google Chirp 3: HD | $30 / 1M chars ([pricing](https://cloud.google.com/text-to-speech/pricing)) | $0.030 | $0.90 | **Not published** — qualitative only | MULAW in the streaming allowlist; **8000 Hz nowhere documented** | 200 RPM Chirp3; **100 concurrent streaming sessions** | gRPC bidi, Pre-GA banner | Instant Custom Voice **allow-listed only**, fixed consent script |
| Google Standard/WaveNet | $4 / 1M chars | $0.004 | $0.12 | — | — | **No streaming support** | No | — |
| Hume Octave | $0.15/1k entry → $0.05 Business | $0.150 → $0.050 | **$4.50** → $1.50 | ~100 ms Octave 2, "not including network transit" | **No.** `format` is `{mp3\|wav\|pcm}` — **no `sample_rate` parameter exists**, and Hume's own docs say µ-law "is not presently supported" | **1 free / 5 Starter / 10 Pro / 20 Scale / 30 Business** | Yes | From 15 s; Octave 2 **still preview** |
| **OpenAI `gpt-4o-mini-tts`** *(incumbent)* | $0.60/1M text tok in, **$12.00/1M audio tok out** ([pricing](https://developers.openai.com/api/docs/pricing)) | **Not derivable — no published char↔token ratio** | **not derivable** | **Not published** | **No — 24 kHz PCM only.** Every byte resampled by `dsp.Downsampler24to8` | RPM/TPM only | **No WebSocket** on `/v1/audio/speech`; SSE + chunked | **Now exists** — custom voice objects, OpenAI-scripted consent recording, ≤30 s sample, 20 voices/org, "eligible customers"; **price not published** |
| OpenAI `tts-1` | $15 / 1M chars | $0.015 | $0.45 | Not published | No | RPM/TPM | Chunked only | No |

**Removed from consideration: PlayHT / Play.ai.** Both `play.ht` and `play.ai` fail DNS
resolution as of 2026-09-09. There is no primary source to cite because there is no vendor.
Do not architect against it.

### The three things this table actually says

1. **The incumbent cannot be costed.** `gpt-4o-mini-tts` bills in audio tokens, OpenAI publishes
   no character-to-token conversion, and the per-minute estimate that used to appear on the old
   pricing layout **is no longer on the current page**. This is a live defect in
   `services/ai_cost.py`: `DEFAULT_RATES["tts.gpt-4o-mini-tts"] = 0.015` is the **`tts-1`
   per-character rate applied to a model that is not billed per character**. Under D14's own
   rule — "a bill that quietly under-reports is worse than one that admits ignorance" — that
   row should be `unrated` today, not confidently wrong.
2. **Only two vendors emit 8 kHz µ-law natively at a price at or below the incumbent's
   character-equivalent**: Azure Neural ($0.015) and Inworld TTS-2 Flash ($0.015). Deepgram
   Aura-2 and Rime Mist v3 do it at $0.030.
3. **Concurrency, not price, is the binding constraint** on the low-latency vendors. At $299/mo
   Cartesia gives **15** concurrent streams and ElevenLabs **30**; Inworld's On-Demand tier
   gives **5**; Hume's free tier gives **1**. Deepgram gives **45 on pay-as-you-go with no
   monthly commitment**. Against `MAX_SESSIONS=4` every one of these clears the bar today — but
   only Deepgram clears it without a subscription, and only Deepgram leaves headroom if D10's
   "raise the limit once you have data" is ever cashed out.

---

## V5 — Speech-to-speech: has D1 shifted?

D1 chose cascaded because the requirement was BYO-LLM, specifically Chinese labs, and "in
speech-to-speech the model *is* the pipeline". That reasoning is unchanged and still correct.
But the economics moved, and honesty requires saying so.

| Option | Price | $/hr (50/50 conversation) | 8 kHz | BYO-LLM |
|---|---|---|---|---|
| **OpenAI `gpt-realtime-2.1`** | $32/1M audio in, $64/1M out, **$0.40 cached in** ([pricing](https://developers.openai.com/api/docs/pricing)) | **~$2.88** | **Yes** — `audio/pcmu`, `audio/pcma` | **No** |
| **OpenAI `gpt-realtime-2.1-mini`** | **$10/1M in, $20/1M out** | **~$0.90** | Yes | **No** |
| Google Gemini Live 3.1 Flash | $0.005/min in, $0.018/min out ([pricing](https://ai.google.dev/gemini-api/docs/pricing)) | ~$1.38 | **No** — 16 kHz in, 24 kHz out | No |
| Amazon Nova 2 Sonic | $3.00/1M speech in, $12.00/1M out (AWS Price List API, us-east-1); eu-north-1 **cheaper** at $2.91/$11.65 | **Not derivable** — AWS publishes no tokens-per-minute conversion | **Yes, 8000 Hz LPCM both directions** — first-class | No |
| **Deepgram Voice Agent, BYO-LLM + BYO-TTS** | **$0.050/min** post-promo ([pricing](https://deepgram.com/pricing)) | **$3.00** | Yes | **Yes** — any OpenAI-compatible endpoint |
| Ultravox | $0.05/min ([pricing](https://www.ultravox.ai/pricing)) | $3.00 | via SIP partners | **Not S2S** — "takes in audio and emits streaming text"; TTS is a third party |
| Kyutai Moshi | Free, self-host | GPU only | 24 kHz | No |

**The finding that matters, and it is uncomfortable:** OpenAI's price drop was real —
their GA post states the 20% cut to $32/$64, and cached audio input fell **$2.50 → $0.40, a
84% cut**, which dominates multi-turn cost because context is re-billed every turn. At
**~$0.90/hr, `gpt-realtime-2.1-mini` is cheaper than the recommended cascade** (V6: $1.36/hr),
accepts G.711 µ-law natively, and does semantic VAD in-model.

**D1 still holds, for the reason D1 gave and no other.** BYO-LLM is the requirement; OpenAI
Realtime has no brain slot, confirmed again today. The cascade is now being paid for in
*money*, not only in complexity — roughly **$0.46/hr of premium** for the right to swap the
model. That is a defensible price for the thing D11 calls diagnosable ("is agent #7 bad because
of its prompt or its model?"), for MiniMax/DeepSeek/Kimi support, and for not handing the
entire conversation to a single vendor. But it should be a known price, not an accident.

The one genuinely new option is **Deepgram Voice Agent with BYO-LLM and BYO-TTS at $0.050/min**
— it is the only managed product in the survey that accepts an arbitrary OpenAI-compatible
endpoint. At **$3.00/hr it is 2.2× the recommended cascade** and collapses the D11 seams that
`providers.py` exists to keep open. Note it as the fallback D11 already anticipated ("leaves
their Voice Agent API as a fallback if the pipeline is ever collapsed"), not as a plan.

---

## V6 — Recommended stack, and the latency budget

### The picks

| Slot | Pick | Why |
|---|---|---|
| **STT** | **Deepgram Flux (`flux-general-en`)** | The only vendor with documented native 8 kHz µ-law *and* semantic end-of-turn *and* 150 concurrent streams on pay-as-you-go. Deletes `dsp.TurnDetector`. |
| **LLM** | unchanged — `OpenAICompatibleLLM`, Western-hosted | Under a cent per hour either way (see below). Not a latency or cost lever. |
| **TTS** | **Deepgram Aura-2** | Native `mulaw`@8000, 45 WSS on PAYG, and D11's original argument still stands: trained on call-centre audio, which is the relevant property for a dispatch business capturing **service addresses and phone numbers**. One vendor, one credential, one latency profile. |

**Runner-up TTS: Inworld TTS-2 Flash** — half the price ($0.015 vs $0.030/1k), native µ-law at
8 kHz, and a claimed 20 ms TTFB. Held back only by **5 concurrent generations on the On-Demand
tier**, which is one above `MAX_SESSIONS=4` and therefore has no headroom at all. At the
Creator tier (10 concurrent) it becomes the cost-optimal choice and saves $0.45/hr. Worth a
bake-off; Aura-2 wins on the number-and-address pronunciation property that has not been
independently tested either way.

### The budget

Caller-perceived latency, from the moment the caller stops speaking to the first audio frame:

| Stage | Now | Projected | Where the saving comes from |
|---|---|---|---|
| Turn detection | **600 ms** (`VAD_END_FRAMES=30`) | **~260 ms** | Flux's EOT replaces the energy hangover. 260 ms is Deepgram's claimed p50; their p90 is ~1 s, so **this is the number most likely to disappoint** |
| STT | **452–583 ms** | **0 ms** | The transcript is already final when `EndOfTurn` fires. This is the whole point of streaming STT and the largest single win |
| LLM → first sentence | ~400 ms | ~400 ms | Unchanged. Already streamed and sentence-split |
| TTS first byte | ~629 ms | **~200 ms** | Aura-2's claimed <200 ms TTFB vs OpenAI's measured ~629 ms |
| Playout priming | **400 ms** | **200 ms** | `PRIME_FRAMES` 20 → 10. Risky and conditional — see below |
| **Caller-perceived total** | **~2300 ms** | **~1060 ms** | |
| **`last_first_audio_ms`** (from turn end) | **~1700 ms** | **~800 ms** | |

**That lands inside the 800 ms–1.2 s target of D15/§14** — but only if two vendor claims hold.

Three honest caveats on this budget:

- **The 260 ms is a p50 claim.** Deepgram publishes p90 ≈ 1 s and p95 ≈ 1.5 s for end-of-turn
  detection. A tail like that means some turns will feel *worse* than the current fixed 600 ms,
  which is at least predictable. `eot_threshold` (default 0.7) is the knob; lowering it trades
  false turn-ends for latency, and `eager_eot_threshold` buys another 150–250 ms at the price
  of **50–70% more LLM calls** — which is a real cost line, not a free option.
- **Halving `PRIME_FRAMES` is the least defensible 200 ms here.** The comment on it records
  that 400 ms of priming "still underran twice in a two-turn call", which is exactly why the
  code buffers whole sentences. Do not touch it until Aura-2's chunk cadence has been measured
  on this box. If it stays at 400 ms the projection is **~1260 ms**, which still beats the
  target's upper bound.
- **Nothing here is measured.** Every millisecond of saving above is a vendor's own claim
  applied to our own measured baseline. The only honest way to book it is the way step 1 and
  step 2 were booked: run it, and record the number.

---

## V7 — Cost model

Assumptions, stated so they can be argued with: a **one-hour conversation**, caller and agent
each speaking ~30 minutes; **~1000 characters ≈ 1 minute of speech** (Inworld's and Hume's own
published convention), so **30,000 characters of TTS per hour**; **20 turns/hour**, each
sending ~800 input tokens (system prompt + `LLM_HISTORY_TURNS=8` window) and producing ~60
output tokens, i.e. **16,000 in / 1,200 out per hour**.

| Stack | STT | LLM | TTS | **$/hr** |
|---|---|---|---|---|
| **Today** (`gpt-4o-mini-transcribe` batch + `gpt-4o-mini` + `gpt-4o-mini-tts`) | 30 min utterance audio × $0.003 = **$0.090** | 16k×$0.15/1M + 1.2k×$0.60/1M = **$0.0031** | **not derivable** (token-billed, no published ratio) | **≥$0.093, TTS unrated** |
| Today, *if* the shipped $0.015/1k rate were right | $0.090 | $0.0031 | 30 × $0.015 = $0.450 | *$0.543 — and this figure is not defensible* |
| **Recommended** (Flux + gpt-5-nano + Aura-2) | 60 min × $0.0077 = **$0.462** | 16k×$0.05/1M + 1.2k×$0.40/1M = **$0.0013** | 30 × $0.030 = **$0.900** | **$1.363** |
| Recommended, Deepgram Growth ($4k/yr) | 60 × $0.0065 = $0.390 | $0.0013 | 30 × $0.027 = $0.810 | **$1.201** |
| **Cost-optimised** (AssemblyAI + gpt-5-nano + Inworld TTS-2 Flash) | **$0.150** | $0.0013 | 30 × $0.015 = **$0.450** | **$0.601** |
| Low-latency premium (Flux + gpt-5-nano + Rime Mist v3) | $0.462 | $0.0013 | 30 × $0.030 = $0.900 | $1.363 |
| **S2S comparison** — `gpt-realtime-2.1-mini` | 600 tok/min in × 30 × $10/1M = $0.180 | *in-model* | 1200 tok/min × 30 × $20/1M = $0.720 | **$0.900** |
| Deepgram Voice Agent, BYO-LLM + BYO-TTS | — | — | — | **$3.000** |

### Two findings worth arguing about

**1. Moving to streaming STT makes STT more expensive, not less — 5×.** Batch STT bills only
the caller's utterances (~30 min/hr); a streaming socket bills the whole hour. $0.090 → $0.462.
That is the honest price of deleting the turn detector, and it is not what "upgrade the STT"
intuitively sounds like. It is still the right trade — 840 ms of caller-perceived latency for
$0.37/hr — but D14's spend cap should be sized knowing it.

**2. The LLM leg is a rounding error.** At **$0.0013–$0.0031 per conversation-hour** it is
~0.2% of the bill. D11's careful reasoning about Western-hosted Chinese models is a **latency**
decision (~200–250 ms/turn from a European host), not a cost one, and should be argued on those
terms. For completeness, if the brain is ever the point: DeepSeek V4 Flash off-peak is
**$0.22/$0.66 per 1M** ([pricing](https://api-docs.deepseek.com/quick_start/pricing)) — and
"peak" is only 01:00–04:00 and 06:00–10:00 UTC on weekdays, so **the entire European business
afternoon is off-peak**.

### Rates for `services/ai_cost.py`

Drop-in replacements for `DEFAULT_RATES`, in the units that module already uses. Ship them as
list prices and check against a real invoice, exactly as D15 instructs.

```python
# STT: per SECOND of audio.  $0.0077/min ÷ 60.
"stt.flux-general-en":        Decimal("0.00012833"),   # Deepgram Flux, PAYG regular
"stt.nova-3":                 Decimal("0.00012833"),
"stt.universal-streaming":    Decimal("0.00004167"),   # AssemblyAI $0.15/hr ÷ 3600
# LLM: per 1000 tokens, split in/out.
"llm.in.gpt-5-nano":          Decimal("0.00005"),
"llm.out.gpt-5-nano":         Decimal("0.00040"),
# TTS: per 1000 characters.
"tts.aura-2":                 Decimal("0.030"),        # Deepgram PAYG
"tts.inworld-tts-2-flash":    Decimal("0.015"),
```

Two code changes this implies, neither cosmetic:

- **`stt_audio_seconds` changes meaning.** `pipeline.py:347` accumulates
  `len(audio) / (8000*2)` — the sum of *utterance* durations, which is correct for batch
  billing and **wrong by ~2× for a streaming vendor that bills socket-open time.** For Flux it
  must become the media session's wall-clock duration.
- **`tts.gpt-4o-mini-tts` should be deleted, not left at 0.015.** With no published
  character-to-token conversion the honest output is an `unrated` row with reason
  `"no rate for model"`, which `ai_cost.py` already produces for free when the key is absent.

---

## V8 — Migration notes

### New classes in `owen-voice/app/providers.py`

The Protocol seams are already the right shape for the TTS swap and the **wrong shape for the
STT swap** — and that is the substantive finding here, not a detail.

| Class | Seam | Fits the existing Protocol? |
|---|---|---|
| `DeepgramTTS` | `TextToSpeech` | **Yes, cleanly.** Implement `synthesize_stream` yielding 8 kHz PCM; `pipeline.py:601` already prefers it via `getattr`. **And it deletes a resampler from the hot path** — `mulaw`/`linear16` at `sample_rate=8000` means no `Downsampler24to8`, no `resample.Decimator`, no 63-tap FIR. That whole class of audio bug ("Metallic — 3-tap box resampler passed 4 kHz at −3.5 dB") stops being reachable. |
| `InworldTTS` | `TextToSpeech` | Yes, same shape. `audioEncoding: MULAW`, `sampleRateHertz: 8000`. |
| `DeepgramFluxSTT` | **`SpeechToText` does not fit** | The Protocol is `async def transcribe(self, pcm8k: bytes) -> str` — an utterance in, a string out. Flux is a **long-lived WebSocket that emits events**. It needs a second Protocol, e.g. `StreamingSpeechToText` with `open()` / `send(frame)` / `events()` yielding `TurnResumed` / `EagerEndOfTurn` / `EndOfTurn`. |

The registry dicts at the bottom of the module (`_STT`, `_LLM`, `_TTS`) extend as-is; `get_stt`
gains a sibling `get_streaming_stt` returning `None` when the configured provider is batch, so
`Conversation` can pick a loop at construction time rather than branching per frame.

**`pipeline.Conversation` is where the real work is.** Today `on_frame` runs
`self.vad.push(pcm)` and dispatches on `("start", …)` / `("end", audio)`. Under Flux the frames
go to the socket and the *vendor* emits those two events — the same two-event vocabulary, from
a different source. Barge-in, the `BARGE_GUARD_MS` window, `HALF_DUPLEX` and
`playout.clear()` all keep working unchanged, because they key off "speech started", not off
who noticed.

### New config knobs in `owen-voice/app/config.py`

```
VOICE_STT_PROVIDER=deepgram_flux          # was: openai
VOICE_STT_MODEL=flux-general-en           # was: gpt-4o-mini-transcribe
VOICE_DEEPGRAM_API_KEY=
VOICE_STT_EOT_THRESHOLD=0.7               # Deepgram default; 0.5-1.0
VOICE_STT_EOT_TIMEOUT_MS=5000             # Deepgram default; 500-60000
VOICE_STT_EAGER_EOT_THRESHOLD=            # unset = eager disabled. Costs 50-70% more LLM calls.

VOICE_TTS_PROVIDER=deepgram               # was: openai
VOICE_TTS_MODEL=aura-2-thalia-en          # was: gpt-4o-mini-tts
VOICE_TTS_ENCODING=mulaw                  # NEW: 8 kHz native, no local resampling
VOICE_TTS_SAMPLE_RATE=8000
```

`VOICE_TTS_INSTRUCTIONS` becomes **dead** under Aura-2 — plain-English delivery direction is a
`gpt-4o-mini-tts` feature. Aura-2 offers `speed` (0.7–1.5) and pronunciation controls instead.
Keep the key, document it as OpenAI-only, and note that its default-off rationale (8 kHz
destroys performed prosody) applies just as hard to any voice you pick here.

### Can `dsp.TurnDetector` be deleted?

The question D15 asked. Per STT choice:

| STT | `dsp.TurnDetector` | Notes |
|---|---|---|
| **Deepgram Flux** | **DELETE** | Native `EndOfTurn`. This is why it wins. |
| **AssemblyAI Universal-Streaming** | **DELETE** | `end_of_turn_confidence_threshold`. |
| **Cartesia Ink-2** | **DELETE** — *if* 8 kHz is confirmed | `turn.end` / `turn.eager_end`. 8000 Hz is not in the API docs; verify first. |
| **OpenAI Realtime transcription** | **DELETE** | `semantic_vad` with `eagerness`. |
| Cartesia Ink-Whisper | **KEEP** | You must send `finalize` yourself. |
| Speechmatics | **KEEP** | Silence timer only, default disabled. |
| Azure Speech SDK | **KEEP** | `azure_semantic_vad` is Voice-Live-only, and Voice Live rejects 8 kHz. |
| Google STT V2 | **KEEP** | No turn detection published. |
| **Any self-hosted option** | **KEEP** | None of them do end-of-turn. This is the central argument of [V3](#v3--self-hosted-stt-on-this-box). |

Even under Flux, **do not delete `dsp.rms_of` or `VAD_MIN_UTTERANCE_RMS`**. The noise floor
exists because "a live call produced `لا لا لا لا` and `Привет` from a quiet line" — that is a
guard against a *model* hallucinating on near-silence, and Flux is still a model. Same for
`looks_like_english`. Delete the turn detector; keep the paranoia it was wrapped in.

Deleting `TurnDetector` also removes `VOICE_VAD_END_FRAMES` and `VAD_SPEECH_RMS` from the
turn-taking path, but `VAD_BARGE_SCALE`, `BARGE_GUARD_MS` and `HALF_DUPLEX` all survive — they
protect against the agent's own voice returning through a speakerphone, which no vendor's EOT
model addresses and which was observed live.

---

## V9 — Risks and caveats

**Pricing volatility, concretely.** Deepgram's streaming prices are explicitly labelled
"limited-time promotional rates" with **no published expiry**, and the Flux TTS promo on the
same page carries a hard date of **2026-09-12 — three days after this document was written.**
Every figure in V7 uses the *regular* rate for exactly this reason. Budget on the regular
column; treat any promo as a windfall.

**Vendor lock-in is real but bounded, and cuts one way.** Choosing Deepgram for both STT and
TTS is the same argument D11 made — one vendor, one credential, one latency profile — and it
concentrates risk. The mitigation is already built: `providers.py` is three Protocols, and the
whole reason D1 chose a cascade is that each stage swaps independently. The *asymmetry* to
watch is that the STT swap needs a **new Protocol shape** (streaming, event-driven), so once
`pipeline.py` is restructured around vendor-emitted turn events, going *back* to a batch STT
means re-adding `TurnDetector`. Keep it in git history, and keep the batch path working behind
`VOICE_STT_PROVIDER=openai` rather than ripping it out.

**Quality at 8 kHz is the great unmeasured thing.** Almost nobody publishes narrowband WER. The
exceptions are damning or unusable: Vosk's own numbers show models at 5.7% on LibriSpeech
landing at **~30% on call-centre audio**; Cartesia's Ink-Whisper publishes **0.19 WER on phone
calls** against 0.015–0.065 on every other category; and the only good telephony number in the
survey — NVIDIA Parakeet's **6.32% on 8 kHz µ-law** — belongs to a model that needs a GPU this
box does not have. Deepgram, AssemblyAI and Speechmatics publish **no telephony WER at all**.
Assume a real accuracy penalty at 8 kHz and measure it on captured call audio before promising
anyone that captures are reliable.

**Self-serving benchmarks, named.** Deepgram's Flux post ranks Flux #1 on a "Voice-Agent
Quality Index" that Deepgram publishes. AssemblyAI claims "41% faster median latency than
Deepgram Nova-3". Cartesia claims Ink-2 at 6.5% WER against "9.2% Deepgram Flux". Three vendors,
three first-party benchmarks, each winning. Deepgram's *quality* methodology is unusually
specific (2,794 three-way preference comparisons over 8,382 samples) — but that methodology
covers preference and pronunciation, **not latency**, and its latency numbers carry no
methodology at all.

**Two vendor pages contradict themselves**, and both matter: AssemblyAI's pricing page says
"100 new streams per minute" while its product page says "unlimited concurrent streams"; Rime's
pricing page says Starter gets 20 concurrent while Rime's own pricing blog says 5. Get either
in writing before depending on it.

**Things that are simply not published**, recorded so nobody re-derives them later: Deepgram
Enterprise rates and concurrency; AssemblyAI volume-discount thresholds, self-hosted GPU specs
and licence terms; Cartesia's self-hosted requirements (docs are login-gated) and instant-clone
pricing; Google's current per-model STT price table (only a **2023** blog post is citable);
NVIDIA AI Enterprise per-GPU pricing; OpenAI's concurrent-session limit for Realtime; OpenAI's
character-to-audio-token ratio; and **an independently measured latency figure for any vendor
in this document**.

---

## What to do next

1. **Get a Deepgram key and run step 2's `/spike/loopback` against Flux + Aura-2.** The
   pipeline already has the harness; `last_first_audio_ms` already exists. One number settles
   V6's entire projection, the same way step 1's 246 frames settled D3.
2. **Measure EOT p50/p90 on this box**, not Deepgram's. The 260 ms is the load-bearing claim.
3. **Fix `ai_cost.py` before the vendor swap, not after** — delete the
   `tts.gpt-4o-mini-tts` rate so today's calls read `unrated` honestly, and change
   `stt_audio_seconds` to session wall-clock when the STT provider is streaming.
4. **Leave `PRIME_FRAMES` at 20** until Aura-2's chunk cadence is observed. It is the one
   saving in the budget that trades against an artefact this codebase has already been bitten
   by twice.

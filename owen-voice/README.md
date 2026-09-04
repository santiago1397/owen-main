# owen-voice — AI voice-agent media service

Step 1 of [`docs/AI_AGENT_SPEC.md`](../docs/AI_AGENT_SPEC.md): **the echo spike**.

It answers the one question that can still invalidate the whole agent design — *does audio
actually leave Asterisk, cross a process boundary, and come back in time to sound like a
phone call?* Everything after this is code this repo has demonstrably written before.

It talks to **nothing**: no OWEN, no database, no AI vendor. A call comes in, its audio is
streamed here over AudioSocket, and every frame is written straight back, so the caller hears
themselves.

## Why a separate container

The worker is capped at `0.5 CPU / 256M` and also runs the ARI consumer every live call
depends on. Audio pipelines there would contend with `recording_fetch`, `transcribe`, the
mail poller and billing — and an audio bug would take down call ingestion platform-wide.
Wrong blast radius (D2).

It uses **its own Stasis app** (`VOICE_ARI_APP=owen-voice`, never OWEN's `ARI_APP`). Two
consumers on one Stasis app fight over the same events. Separate apps mean this service can
be started, crashed and restarted with zero effect on live calls.

## What it required from the host: nothing

- **No `extensions.conf` change.** Channels are originated with ARI's `app` parameter, which
  drops them straight into our Stasis app, bypassing the dialplan. (`asterisk/README.md`
  warns that file is rendered from templates and was once silently corrupted by a bare
  `envsubst` — worth not touching.)
- **No `ari.conf` change.** It reuses the existing ARI user.
- **No firewall change.** The AudioSocket port is published on **loopback only**; Asterisk is
  native on this host and reaches it at `127.0.0.1:9092`.

Verified present on the box (2026-09-03): `app_audiosocket.so`, `chan_audiosocket.so`,
`res_audiosocket.so`, `res_ari_channels.so` — all loaded and Running.

## Layout

| File | Role |
|---|---|
| `app/audiosocket.py` | **Pure** wire codec — framing only, no I/O. Unit-tested with no Asterisk. |
| `app/session.py` | Per-call sessions + the UUID→session registry, and the counters that make the result unambiguous |
| `app/ari.py` | Thin ARI client — only what the spike needs (deliberately not a fork of the backend's 1,146-line client) |
| `app/server.py` | The TCP listener and the echo loop. **This is the only file step 2 replaces.** |
| `app/main.py` | Control API + ARI event consumer + server startup |
| `tests/test_audiosocket.py` | 29 checks over the framing — run with no dependencies |

## Run the tests (no Docker, no Asterisk)

```bash
cd owen-voice && python -m tests.test_audiosocket
```

Covers the failure that matters and is invisible when it goes wrong: TCP gives no framing
guarantees, so frames split across reads, several frames per read, and byte-at-a-time
delivery are all exercised. A mis-split frame is a click in someone's ear, not an exception.

## Deploy + run the spike

```bash
# on the VPS, from /opt/santiagoproperties/owen-main
git pull --ff-only
docker compose --env-file .env.prod build owen-voice
docker compose --env-file .env.prod up -d owen-voice

# 1. Is it alive and can it see ARI?
curl -s localhost:8099/health | python3 -m json.tool
#    expect: "ari_reachable": true, "stasis_app": "owen-voice"

# 2. Call YOUR phone and echo your voice back to you.
curl -s -X POST localhost:8099/spike/call \
     -H 'content-type: application/json' \
     -d '{"to":"+1XXXXXXXXXX"}' | python3 -m json.tool

# 3. Answer, say something, hang up. Then read the verdict:
curl -s localhost:8099/sessions | python3 -m json.tool
```

> The compose file always needs `--env-file .env.prod`, or Traefik's `Host()` labels resolve
> empty and break API routing for the whole stack.

> `/spike/call` rings **your own phone** to prove a transport. That is not agent outbound
> calling, which `AI_AGENT_SPEC` scopes out entirely (see Scope / D8).

## Reading the result

`/sessions` returns counters and a plain-English `verdict`, so "I heard nothing" resolves
without a packet capture:

| Verdict | Means |
|---|---|
| `OK — audio flowed both ways` | ✅ Step 1 passes. Proceed to step 2. |
| `asterisk never connected …` | `externalMedia` failed, or `VOICE_AUDIOSOCKET_ADVERTISE` doesn't match the published port |
| `connected but no audio received …` | The media channel isn't in the bridge — check ARI logs for a 409/422 on `addChannel` |
| `audio received but digital silence …` | Wrong media format, or the caller leg isn't bridged |
| `audio received but nothing echoed back …` | The write path failed |

`peak_amplitude` (0–32767) is the single most useful number: it separates *no audio* from
*silent audio*, which are completely different bugs.

## If `encapsulation=audiosocket` is rejected

The modules are present, so this is unlikely — but the documented fallback (D3) is the
dialplan `AudioSocket()` application via `app_audiosocket.so`, **not** RTP. Hand-rolling a
jitter buffer is the larger risk. That fallback does cost one `extensions.conf` line.

## What step 2 changes

Only `server.py`'s echo. The frame loop becomes:

```
rx audio ─▶ STT (Deepgram Flux) ─▶ LLM (OpenAI-compatible) ─▶ TTS ─▶ tx audio
```

Framing, correlation, teardown and counters stay exactly as they are.

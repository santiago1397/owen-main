# SignalWire Call Flow Builder Ingestion — Bottleneck & Resolution

Written 2026-07-16 after a live debugging session getting the first real SignalWire
test number (`CL Ads 3`, `+19546347370`, campaign `Craiglist`) working end to end.

**UPDATE (same day, later session):** the original version of this doc ended with
several open items. A follow-up session checked SignalWire's actual documentation
against those items and found a clean resolution — **the modern Voice API
(`GET /api/voice/logs` + `.../events`) fully replaces the dead Compatibility API**,
requires zero Call Flow Builder node configuration, and scales to every number
automatically. This has been implemented, deployed, and verified end to end with
real data (see "Resolution" section near the top). The rest of the document below
is kept as-is as a record of the investigation — the Call-Flow-Builder-node-based
workaround it describes (Call State URL, Request node, `tracking_number` query
param) is now **redundant** but still deployed and harmless (belt-and-suspenders).
It can be simplified away in a future session if desired.

---

## Resolution: the modern Voice API replaces the dead Compatibility API

`GET https://{space}/api/voice/logs` (paginated, `page_size`/`links.next`) lists
every call leg — inbound and outbound — for the whole project, each with the
**correct** `from`/`to`/`direction`/`status`/`duration`/`created_at`/`parent_id`.
For the real inbound leg, `to` is already the actual tracking number that was
dialed — no leg-correlation trick needed, just filter `direction == "inbound"`
(exactly like the existing `_is_inbound` helper already does for Twilio).

`GET .../api/voice/logs/{id}/events` gives that call's full event timeline,
including a `calling_call_record` event (`state: "finished"`) carrying the
recording's `url`, `duration`, and `recording_id` directly — **no Call Flow
Builder node configuration required at all** for either call state or recordings.
(There is no standalone `/api/voice/recordings` resource — confirmed 404 — so
recordings are read from each call's own event timeline instead.)

Implemented in:
- `backend/app/providers/signalwire_client.py` — `fetch_recent_voice_logs`,
  `fetch_recent_calls_voice_logs`, `fetch_recordings_via_voice_logs`. The old
  Compatibility-API functions (`fetch_recent_calls`, `fetch_recent_recordings`) are
  left in place, unused for this account, in case some other SignalWire account
  uses classic LaML-configured numbers instead.
- `backend/app/workers/reconciler.py` — `_SOURCES["signalwire"]` and
  `_RECORDING_SOURCES["signalwire"]` now point at the Voice-Logs-based functions.

**Verified live**, no new call needed (this is a pure REST poll against existing
history): `reconcile-now --hours 6` pulled **25 real inbound calls across the whole
account** (other campaigns' numbers too, not just the test number) in one run, with
zero errors. Confirmed against `CL Ads 3` specifically: all 4 real test calls made
during the original debugging session were correctly ingested — right status,
right duration, right campaign attribution — and all 5 recordings across the
session were downloaded, transcribed, and analyzed successfully. One transcript
came back correctly in Spanish ("Buenas, probando sonido, probando sonido. Muchas
gracias.") and MiniMax correctly analyzed it (`is_spam: false`, `category: other`,
accurate summary), proving the full pipeline — reconciler → download → Whisper →
MiniMax — works end to end with zero Call Flow Builder webhook configuration.

This also resolves the open scalability question (open item #2 below): since the
Voice Logs API is account-wide and requires no per-number setup, **one shared flow
can serve any number of tracking numbers** with zero incremental configuration per
number. The `tracking_number` query-param hack is no longer needed for new numbers
going forward.

### What's now genuinely optional vs. still needed

- **Reconciler (Voice Logs polling)**: now the **primary**, fully automatic path.
  Works for every number with zero per-number setup.
- **Call Flow Builder webhook workaround** (Call State URL, Request node,
  `tracking_number` param, Basic Auth secret): still deployed, still fires, still
  harmless (idempotent — same data, ingested twice, no duplication issues). Gives
  near-real-time updates vs. the reconciler's poll interval. Worth keeping *if*
  near-real-time matters; otherwise it's safe to rip out and simplify the flow back
  down to `Handle Call → Answer Call → Play Audio/TTS → Start Call Recording →
  Forward to Phone` with no extra nodes, relying entirely on the reconciler.
- **Recording Request-node body correction** (documented variables `%{call.from}`,
  `%{call.to}`, `%{call.call_id}`, `%{record_call_url}` via a `Stop Call Recording`
  node) was researched and would have fixed the webhook path's recording capture —
  but turned out to be unnecessary once the Voice Logs API proved sufficient on its
  own. Documented here only in case near-real-time webhook delivery is later judged
  worth keeping.

---

## Original investigation (kept for the record)

This documents the root architectural problem, everything tried, what was deployed
as a workaround before the resolution above, and what was still unresolved at that
point — kept for a fresh session's context on how we got here.

## TL;DR

This app (`workers/reconciler.py`, `providers/signalwire_client.py`) was built
assuming SignalWire's classic Twilio-compatible **Compatibility API**
(`Calls.json` / `Recordings.json`) would reflect all calls, polled every 5 minutes.
**It doesn't, for this account.** Numbers here are routed through SignalWire's
**Call Flow Builder** (a modern, Relay/Calling-API-based product), and calls routed
that way never appear in the classic Compatibility API log — confirmed empirically,
not theoretically (see "The core discovery" below). The 5-minute reconciler has
therefore never successfully ingested a single SignalWire call, ever, no matter how
long it runs.

The current fix is a webhook-based workaround bolted onto Call Flow Builder's
limited hook points, using a different event schema than this codebase was written
for, with authentication and attribution problems of its own. It works for a single
test number as of this writing (pending final live-call verification) but has an
unresolved scalability question for many numbers. See "Open items" at the end.

---

## The core discovery: Compatibility API is a dead end for this account

Verified by direct API calls against `Accounts/{project_id}/Calls.json` and
`.../Recordings.json` (`SIGNALWIRE_SPACE_URL=expeditors.signalwire.com`,
`SIGNALWIRE_PROJECT_ID=15082c33-dd5e-4dfb-8ce7-3d5457b0f017`):

- Querying **unfiltered, account-wide, no date restriction** → **zero results**,
  for both Calls and Recordings.
- This account has ~69 real, actively-used numbers (Yelp/GBP ad campaigns for real
  businesses — roofing companies, garage door repair, locksmiths — going back to
  January 2026). If the Compatibility API worked for this account at all, it would
  show *something*.
- Confirmed a real call **did** reach SignalWire's platform (visible in the
  dashboard's own Voice Logs) while the Compatibility API simultaneously showed
  nothing for it, before and after.

**Conclusion:** Call Flow Builder does not write to the classic Compatibility API
call/recording log for this account. This isn't a timing issue, a pagination bug, or
a wrong-credentials issue (the same credentials correctly list all 69
`IncomingPhoneNumbers`). It's an architectural mismatch between what this app was
built to poll and how this SignalWire account actually routes calls.

**Impact:** `workers/reconciler.py`'s SignalWire polling (`_SOURCES["signalwire"]`,
`_RECORDING_SOURCES["signalwire"]`) — which `ARCHITECTURE.md` describes as the
*primary* ingestion path for SignalWire specifically because Call Flow Builder
"doesn't reliably POST callbacks" — cannot work at all for this account. Webhooks
turned out to be not just more reliable but the *only* viable path.

---

## Dead ends tried (for the record, so they aren't retried)

- **SignalWire dashboard's Call Flow Builder "test call" / simulator widget**:
  doesn't place a real PSTN call. No `Call`/`Recording` resource is ever created,
  nothing shows in Voice Logs, nothing reaches any webhook. Looked like a real test
  but wasn't — cost real debugging time before we figured this out.
- **Number's "Edit Settings" page**: only exposes "Assigned Resource" (which flow
  handles the number). No status-callback/webhook URL field at the number level in
  this account's UI (unlike classic Twilio number config).
- **"Start Call Recording" node**: no callback/status-URL field of any kind.
- **"Answer Call" node**: no Call State URL field either (checked specifically
  because it would report the *inbound* leg, which is what we actually need — see
  "Attribution problem" below).

---

## What's currently deployed (the workaround)

### 1. Call status via "Forward to Phone" → Call State URL

The only native webhook-capable field found anywhere in the flow builder is on the
**"Forward to Phone"** node: **"Call State URL"**, firing on
Created/ringing/answered/ended. Configured to:

```
https://cfb:<SIGNALWIRE_CFB_WEBHOOK_SECRET>@api.<domain>/webhooks/signalwire/status?tracking_number=%2B19546347370
```

Two problems with this, both worked around:

**(a) It reports the wrong leg.** This event fires for the *outbound/forward* leg
(the leg to the real business line), not the original *inbound* leg to the tracking
number. Its payload:

```json
{
  "event_type": "calling.call.state",
  "params": "{...stringified Python dict, NOT valid JSON — single-quoted...}",
  "space_id": "...", "project_id": "...", "timestamp": "..."
}
```

`params`, once parsed (`ast.literal_eval`, **not** `json.loads` — it's a Python
`repr()`, not JSON), looks like:

```python
{
  'call_id': '<this leg's own throwaway id>',
  'segment_id': '<same as call_id>',
  'call_state': 'created' | 'ringing' | 'answered' | 'ended',
  'parent': {'call_id': '<the ORIGINAL inbound call's id>', ...},
  'direction': 'outbound',
  'device': {'params': {
      'from_number': '<the real caller's number>',
      'to_number': '<the forward target, NOT the tracking number>',
  }},
  'end_reason': 'hangup' | 'busy' | 'no_answer' | 'cancel' | 'timeout' | 'error',
  'start_time': <epoch ms>, 'answer_time': <epoch ms>, 'end_time': <epoch ms>,
}
```

Fix: `backend/app/providers/signalwire.py` (`SignalWireAdapter.parse_status_event`)
detects `event_type == "calling.call.state"`, parses `params` via
`ast.literal_eval`, and uses **`parent.call_id`** (not the leg's own `call_id`) as
the canonical call SID — so this forward-leg event correctly updates the same `Call`
row the inbound leg owns. `call_state`/`end_reason` are mapped to this app's
internal status vocabulary (`_CALL_STATE_TO_STATUS`, `_END_REASON_TO_STATUS`).

**(b) It doesn't know the tracking number.** The payload's own `to_number` is the
forward target (e.g. the business's real cell phone), not the tracking number that
was actually dialed — so campaign/number attribution can't be derived from this
event's own fields. See "Attribution problem" below.

This whole schema is completely different from the Twilio-style `CallSid`/
`CallStatus`/`From`/`To` form fields this codebase's `TwilioAdapter` (and, by
inheritance, the old `SignalWireAdapter`) was built to parse via the classic
Compatibility-API-style webhooks. That old parser is still used as a fallback for
any future classic LaML-style webhook config, but it's dead code for this account
today.

### 2. Recording capture via a manually-added "Request" node

"Start Call Recording" has no callback field at all, so recording completion has to
be reported some other way. Added a generic **"Request"** node to the flow, wired in
after **"Forward to Phone"** on *both* the "no answer" and "success" output
branches (so it fires regardless of whether the forwarded call was picked up),
pointed at:

```
https://cfb:<SIGNALWIRE_CFB_WEBHOOK_SECRET>@api.<domain>/webhooks/signalwire/recording?tracking_number=%2B19546347370
```

with a JSON body using SignalWire's `%{...}` template-variable syntax, guessed
(not confirmed against real documentation or a variable picker) as:

```json
{
  "CallSid": "%{call.sid}",
  "RecordingSid": "%{call.recording.sid}",
  "RecordingUrl": "%{call.recording.url}",
  "RecordingStatus": "completed",
  "RecordingDuration": "%{call.recording.duration}"
}
```

**This has not yet been verified against a real payload.** As of writing, no live
call has completed all the way through with this node's current config, so we don't
actually know whether these variable names resolve to real values, resolve to
literal unresolved strings (e.g. `"%{call.sid}"` verbatim), or something else. The
existing Twilio-style parser (`TwilioAdapter.parse_recording_event`, inherited by
`SignalWireAdapter`) is what will parse this body — it expects exactly the field
names shown above.

A defensive fallback was added in case `CallSid` doesn't resolve to anything usable:
`webhooks/common.py`'s `/recording` handler checks whether the resolved
`provider_call_sid` is empty or contains an unresolved `%{` template marker, and if
so, falls back to `_fallback_call_sid()` — looks up the most recent `Call` row for
the `tracking_number` query param and uses its SID instead. This is a
recency-based heuristic, not a real correlation — fine for low-volume testing, not
guaranteed correct if a number gets overlapping calls in quick succession.

### 3. Authentication: Basic Auth instead of signatures

None of Call Flow Builder's generic nodes can produce Twilio's classic
HMAC-SHA1 webhook signature (`core/security.py: verify_twilio_signature` /
`verify_signalwire_signature`, keyed on `TWILIO_AUTH_TOKEN` / `SIGNALWIRE_AUTH_TOKEN`)
— that scheme requires signing over the exact form body, which a generic
"make an HTTP request" node has no concept of. Worked around with a **separate**
authentication path: HTTP Basic Auth credentials embedded directly in the node's URL
field (`https://user:SECRET@host/path` — the node natively supports this syntax,
confirmed from its own URL-field placeholder), checked in
`webhooks/common.py: _cfb_basic_auth_ok()` against a new setting
`SIGNALWIRE_CFB_WEBHOOK_SECRET` (generated once, stored in `.env.prod`).

This is deliberately permissive: **any** request presenting the correct Basic Auth
password is accepted, with no signature verification of the body contents at all.
That's an accepted tradeoff given CFB's capabilities, but worth flagging as a
security posture decision for the next session to sign off on explicitly — it's
essentially "bearer secret in a URL, over HTTPS," not a proper per-request signature.

### 4. Attribution problem: which tracking number was this?

Neither the Call State event nor the (unverified) recording event reliably carries
the *original tracking number that was dialed* — the Call State event's `to_number`
is the forward target; the recording event doesn't have a documented number field at
all. Workaround: hardcode it as a **query-string parameter** on each flow's webhook
URLs (`?tracking_number=%2B19546347370`), extracted in `webhooks/common.py` and
injected into the parsed params so the status parser can override `to_number` with
the real tracking number rather than trusting the payload.

**This is the open scalability question** (see below) — it only works cleanly if
every number has its own dedicated flow with its own correctly-set query param.

---

## Unrelated infra bugs found and fixed along the way

These aren't part of the SignalWire architecture problem, but came up while
debugging it and are worth knowing about:

1. **Alembic was silently disabling all application logging, permanently, on every
   container start.** `alembic/env.py` calls `logging.config.fileConfig()`, whose
   default `disable_existing_loggers=True` disables every Python logger *not*
   explicitly named in `alembic.ini` — including `uvicorn`, `uvicorn.access`, and
   every custom logger in this app (`webhooks`, `ingestion`, `worker`, etc.) — for
   the rest of the process's life. Since `run_migrations()` runs at startup in both
   `app` and `worker`, this meant **no application logs of any kind** were ever
   visible in `docker logs`, for anything, the whole time this app has been
   deployed — not just newly-added debug logging. Fixed in
   `backend/app/migrate.py`: after migrations run, iterate
   `logging.Logger.manager.loggerDict` and re-enable every logger
   (`.disabled = False`), then reassert `logging.basicConfig(force=True)`. Confirmed
   fixed live (uvicorn access logs and app logs now appear correctly).

2. **Reconciler had no per-provider error isolation.**
   `workers/reconciler.py: reconcile_recent()` iterated providers in a fixed
   dict order (`twilio` before `signalwire`) with no try/except. Since
   `TWILIO_ACCOUNT_SID=CHANGE_ME` (never configured — this deployment is
   SignalWire-only), every single reconcile run threw a 401 on the Twilio call and
   aborted **before ever reaching SignalWire**. This means the reconciler had never
   successfully polled SignalWire even once, silently, since this app was deployed —
   compounding the Compatibility-API dead-end above. Fixed by wrapping each
   provider's fetch in try/except so one provider's failure doesn't block others.

3. **Any ad hoc `docker compose` command missing `--env-file .env.prod` silently
   breaks public API routing.** `docker-compose.prod.yml` uses `${APP_DOMAIN}` in
   Traefik labels (`Host(\`api.${APP_DOMAIN}\`)`) and in the frontend's
   `VITE_API_BASE` build arg. Docker Compose only substitutes `${...}` from a
   `.env` file (doesn't exist in this repo) or the invoking shell's *exported*
   environment — **never** from `env_file:` (which only injects vars into the
   container's own runtime, not into Compose's own variable substitution for
   labels/build-args). The official deploy path (`scripts/deploy.sh`) always
   includes `--env-file .env.prod` explicitly, so real deploys are unaffected — but
   running `docker compose build/up` directly without that flag (as happened
   repeatedly during this debugging session) silently bakes in a broken
   `Host(\`api.\`)` rule, breaking **all** public API routing (falls through to
   Traefik's default 404) until the container is recreated correctly. Worth
   hardening so this can't happen by accident — e.g. a wrapper script, or Makefile
   targets that always pass the flag (worth double-checking they do).

4. **Production transcription was silently running on fake data.**
   `TRANSCRIPTION_ENGINE` defaulted to `"dummy"` (hardcoded canned transcript,
   `analysis/transcription.py: DummyTranscriptionEngine`) and was never set in
   `.env.prod` — every real call's "transcript" was the identical fake sentence,
   dutifully analyzed by MiniMax as if it were real. Fixed: `TRANSCRIPTION_ENGINE=openai`
   + a real `OPENAI_API_KEY` now set in `.env.prod`, confirmed picked up by the
   running containers.

---

## Current deployed state (files touched)

- `backend/app/providers/signalwire.py` — native `calling.call.state` parser.
- `backend/app/webhooks/common.py` — Basic-Auth bypass (`_cfb_basic_auth_ok`), JSON
  body support (previously form-only), `tracking_number` query-param extraction,
  recording `CallSid` fallback correlation (`_fallback_call_sid`).
- `backend/app/core/config.py` — new `SIGNALWIRE_CFB_WEBHOOK_SECRET` setting.
- `backend/app/migrate.py` — logging re-enable fix.
- `backend/app/workers/reconciler.py` — per-provider try/except isolation.
- `.env.prod` / `.env.prod.example` — `TRANSCRIPTION_ENGINE=openai`,
  `OPENAI_API_KEY` set, `SIGNALWIRE_CFB_WEBHOOK_SECRET` set (example file documents
  the key, not a real value).
- DB: `Craiglist` campaign, `+19546347370` number ("CL Ads 3") registered via
  `app.scripts.manage`, provider `signalwire`, no `forwards_to` set.
- SignalWire flow (CL Ads 3): `Handle Call → Answer Call → Play Audio/TTS →
  Start Call Recording → Forward to Phone` (Call State URL: Basic Auth +
  `tracking_number` param) `→ Request node` (wired from both "no answer" and
  "success" branches of Forward to Phone; same auth + tracking param; body/variable
  correctness **unverified**).

The status-event parsing path (native schema → correct correlation → correct
attribution) has been validated by replaying a real captured payload through the
actual ingestion code (not just unit-tested in isolation) — confirmed it produces a
correctly-attributed `Call` row. The recording path has **not** been validated with
real data yet.

---

## Open items — status after the follow-up session

1. ~~Verify the recording Request node with a real completed call~~ — **superseded.**
   The Voice Logs API makes the Request-node recording path unnecessary; verified
   the recording pipeline end to end via the reconciler instead (see "Resolution").
   The documented correct variables for the Request-node approach, if ever wanted
   for near-real-time delivery, are recorded above.

2. ~~Solve multi-number scalability for attribution~~ — **resolved.** The Voice Logs
   API reports the correct `to` (tracking number) per inbound leg directly,
   account-wide, with zero per-number/per-flow configuration. No query-param hack
   needed for numbers onboarded going forward.

3. **Still open: revisit the Basic-Auth-secret security posture** on the (now
   optional) webhook path — currently accepts any payload shape from anyone with the
   shared password, no signature verification of contents. Decide if that's
   acceptable to keep running alongside the reconciler, or if it should be removed
   now that it's not load-bearing.

4. ~~Decide the fate of `workers/reconciler.py`'s SignalWire polling~~ — **resolved,
   inverted.** It's no longer dead — it's now the primary, fully-automatic path,
   repointed at the Voice Logs API (`fetch_recent_calls_voice_logs`,
   `fetch_recordings_via_voice_logs`). The old Compatibility-API functions are kept
   unused in `signalwire_client.py` for any future classic-LaML account.

5. **Still open: harden deploy tooling** so `docker compose` can't silently run
   without `--env-file .env.prod` (see infra bug #3) — a wrapper script or clearer
   Makefile/README guardrail.

6. **Still open, now lower stakes:** the recording `CallSid` fallback correlation
   (`_fallback_call_sid`, recency-based) only matters for the now-optional webhook
   path. Fine to leave as is unless that path is kept long-term for near-real-time
   delivery.

7. **New:** decide whether to keep the Call-Flow-Builder webhook path at all
   (Call State URL + Request node + Basic Auth secret) now that the reconciler
   handles everything reliably on its own. Keep it only if near-real-time
   (sub-5-minute) visibility is actually needed; otherwise simplify the flow back
   down and remove `SIGNALWIRE_CFB_WEBHOOK_SECRET`/`_cfb_basic_auth_ok` to reduce
   attack surface.

8. **New:** consider tuning `RECONCILE_WINDOW_HOURS`/poll frequency now that the
   reconciler is confirmed to be the primary ingestion path for this account (it
   currently runs every 5 minutes via APScheduler, `worker.py`) — and consider
   whether newly-discovered numbers (the 25 real calls pulled in belong to numbers
   with no registered `Campaign`/`Number` row yet) should be auto-registered or
   left unattributed until a human maps them.

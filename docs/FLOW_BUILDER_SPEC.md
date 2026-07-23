# Flow Builder — SignalWire-parity spec (Tickets 15–17)

Goal: make OWEN's flow subsystem a full replacement for SignalWire's Call Flow Builder,
for **manually-managed BulkVS/Asterisk numbers only** (runtime already filters
`media_provider == "asterisk"`). Twilio/SignalWire paths are untouched.

Design decisions were resolved interactively on 2026-07-23. Anything not listed here
keeps its current behavior.

Explicitly out of scope: Execute SWML (SignalWire-proprietary), speech-input gather
(DTMF only in v1), Start/Stop Recording as node types (`record` stays a modifier flag).

---

## Ticket 15 — Phase 1: runtime core

Make the existing graph format actually executable on live calls.

### 15.1 Real DTMF gather
- `AsteriskAriClient.read_digit` currently plays the prompt and returns `None`
  (`backend/app/providers/asterisk_client.py`).
- The ARI events WebSocket consumer (`backend/app/workers/asterisk_consumer.py`) must
  correlate `ChannelDtmfReceived` events to the waiting interpreter: a per-channel
  registry of `asyncio.Queue`s (channel_id → queue) that the consumer feeds and
  `read_digit` awaits on with the node's `timeout` (default 5s).
- `menu` node semantics: collect up to `max_digits` (default 1); return the digit
  string; port routing unchanged (`digits`/`timeout`/`invalid`).

### 15.2 TTS for prompts
- New `backend/app/services/tts.py`: OpenAI TTS (`tts-1`, default voice `alloy`,
  configurable via `TTS_VOICE`/`TTS_MODEL` settings). Output WAV 8kHz mono (slin
  compatible; use ffmpeg resample like the stereo-split code does) stored under
  `<RECORDINGS_DIR>/tts/<sha256(text|voice)>.wav`.
- Synthesis triggers:
  - at **activation** (`POST /flows/{id}/versions/{vid}/activate`): synthesize every
    static prompt in the graph, best-effort (log failures, do not block activation);
  - **lazily at call time** when the cached file is missing or the text contains
    `{{...}}` interpolation (Phase 3).
- Playback: interpreter passes `sound:` URI pointing at the cached file. Asterisk must
  be able to read the file — the recordings volume is shared with the host; the sounds
  dir must be added to the asterisk container/host path config (document in
  `asterisk/README.md`; the deploy renders configs with envsubst).
- If TTS fails at call time: skip playback, continue the flow (never dead air).

### 15.3 Real Forward-to-Phone (dial + bridge)
- Replace the fake originate-only `dial_number`: originate the outbound leg (BulkVS
  PJSIP trunk), create an ARI mixing bridge, add both legs on answer.
- Caller ID: **passthrough of the original caller's number by default**; per-node
  `caller_id` config may override with a literal.
- Answer detection: timeout-only. Ring the target `timeout` seconds (node config,
  default 25). Outcomes → ports: answered (bridged), `busy`, `noanswer` (timeout),
  `failed` (originate error). Carrier voicemail counts as answered — accepted for v1.
- On bridge: the interpreter blocks until either leg hangs up, then ends (the flow does
  not continue after a successful bridge unless a port is wired — `answered` port wired
  means post-call continuation is intentional; default: end).
- `dial_operator` group bridging stays a follow-on; do not regress it.

### 15.4 ai_agent port fix
- Validator allows `{default, transfer, complete}`; engine returns
  `{transfer, end_call, default, failed}`. Align both on
  `{default, transfer, complete, failed}` and map engine `end_call` → `complete`.

### 15.5 Number assignment
- `PATCH /api/numbers/{id}` accepts `flow_id` (nullable to unassign).
- Guard: assignable only if the flow has `active_version_id IS NOT NULL` (400 otherwise).
- Only numbers with `media_provider == "asterisk"` accept a flow (400 otherwise).
- Frontend: replace the disabled "Assign flow (coming soon)" button on NumberDetail
  with a dropdown of active flows; show assigned numbers on the flow detail page.

### 15.6 Safety net
- New setting `FLOW_FALLBACK_FORWARD_NUMBER` (global, optional). If a call enters
  Stasis for a flow-assigned number and the flow fails to load/crashes at the top,
  blind-forward the caller to this number instead of dead air. Per-number override is
  a later enhancement.

### 15.7 Timeline visibility
- Ensure `flow.node.*` call_events render in the Calls detail drawer timeline (they are
  the flow debugger).

## Ticket 16 — Phase 2: React Flow canvas

- `@xyflow/react` canvas replaces the rule form entirely (rule form code removed;
  `flowGraph.ts` parse/build retired). Existing graphs open on the canvas; graphs
  without layout metadata get auto-layout (dagre or elk, LR).
- Node palette mirroring current NODE_TYPES: entry (fixed, one per flow), play, hours,
  menu, dial, voicemail, ai_agent, hangup. `record` + `consent_notice` exposed as a
  checkbox/textarea on node config panels (modifier, not a node).
- Node config side panel per type (prompt text, hours schedule editor, menu digit rows,
  dial target/caller_id/timeout, agent picker, voicemail greeting).
- Layout persistence: store node positions in the graph JSON under a `layout` key
  (`{node_id: {x, y}}`) — additive; validator ignores it.
- Save draft → existing `POST /versions`; Activate → existing activate endpoint;
  render its `{errors, warnings}` detail inline (errors block, warnings badge).
- Version history: side panel listing versions; open old version read-only; "Restore"
  saves it as a new version. Active/draft badges as today.

## Ticket 17 — Phase 3: parity nodes

- **Variable store**: per-call in-memory dict on the interpreter; built-ins
  `caller_number`, `dialed_number`, `call.time`, `call.dow`, `gather.digits`,
  `request.status`, `request.body.*` (dot-path into parsed JSON). Snapshot relevant
  vars into `flow.node.*` event payloads.
- **`{{var}}` interpolation** in: TTS prompt text, SMS body/to, Request URL/headers/
  body, dial target. Unknown vars interpolate to empty string.
- **set_vars / unset_vars nodes**: rows of `name = literal-or-{{var}}`; port `default`.
- **conditions node**: ordered rows `{variable, operator, value, port}`; operators
  `equals, not_equals, contains, regex, gt, lt, is_empty`; first match wins; `else`
  port required by validator. No JS eval, ever.
- **send_sms node**: `to` (default `{{caller_number}}`), body with interpolation, from
  = the flow's DID. Fire-and-forget through the existing outbound SMS service (opt-out
  + 10DLC gating apply). Port `default` taken immediately.
- **request node**: GET/POST, optional headers, JSON body, 5s hard timeout; response →
  `request.status` / `request.body.*`; ports `success` (2xx) / `failure`.
- Validator: add the new node types + ports; canvas: add palette entries + config panels.

## Rollout (after Ticket 15 ships)

1. Dedicated test BulkVS DID (provision one if none spare).
2. Recreate the SignalWire "DTR-rec-test" flow on it: greeting → menu → consent
   message (record on) → forward to cell.
3. Real-call checklist: menu digits route; bridge connects with caller-ID passthrough;
   recording lands, transcribes, analyzes; GHL relay fires; timeline shows node path.
4. Port live numbers one at a time, lowest-volume campaign first. SignalWire flows stay
   deployed until their replacement passes the checklist.

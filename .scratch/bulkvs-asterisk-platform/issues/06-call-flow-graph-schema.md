# Call-flow graph representation schema

Type: grilling
Status: closed
Assignee: svillahermosa
Blocked by: 02, 04

## Question

Design the stored **flow-graph** that declarative rule forms produce today and a visual builder edits later,
and that the ARI Stasis app **executes**. This is the core abstraction the whole product hangs on.

- What node/edge vocabulary covers the destination's handlers: **forward** (single / ring group / sequential),
  **record** (toggle on any leg), **voicemail**, **business hours** branch + after-hours fallback, **IVR menu**
  (DTMF gather → branch), **AI agent** handoff, hangup? Keep it minimal but composable.
- JSON graph vs. normalized tables? Versioning/immutability (a running call should pin the flow version it started on).
- How a rule form maps onto the graph (so both authoring modes share one representation).
- How the ARI executor walks the graph at runtime (interpreter design, per-node ARI operations, error/timeout edges).
- Validation rules (no dangling nodes, exactly one entry, terminal coverage).

Use `/grilling` + `/domain-modeling`; produce the schema + a couple of worked example flows.

## Answer (resolved 2026-07-22, via `/grilling` + `/domain-modeling`)

**Model:** a true directed graph, stored as a `jsonb` body inside a relational, append-only version
envelope; interpreted **in-memory** by the ARI Stasis app, which emits a `call_event` at every node
transition. Authored through constrained forms now; a Twilio-Studio-style visual builder is the real
end-state authoring model (fog for the UI, but the schema is built for it now — no linear-form ceiling).

### Decision log (8 questions, all resolved with the recommended option)
1. **Structure — true directed graph** (typed nodes + named output edges), *not* a linear rule list.
   Only a graph expresses a nested IVR menu + multi-step handoffs. Authored via forms that emit graph
   fragments, so the operator never sees raw graph until the visual builder ships. One representation,
   two front-ends.
2. **Storage — hybrid (C):** relational `flows`/`flow_versions` envelope for identity + lifecycle;
   graph body as `graph jsonb` (read/written as a whole unit by executor + builder; mirrors
   `call_analysis.tags` / `transcriptions.words`). Edge integrity comes from validation-at-save (Q8),
   not FKs.
3. **Versioning — append-only `flow_versions`**, immutable, same instinct as `call_events`-as-truth.
   `flows.active_version_id` points at the live version; a call stamps `flow_version_id` at
   `StasisStart` and reads that frozen row for its whole life (like `campaign_id` at ingest).
   Number → flow → active_version, resolved to a concrete `flow_version_id` at call start.
4. **Node vocabulary:** `entry`, `play`, `hours`, `menu`, `dial`, `voicemail`, `ai_agent`, `hangup`.
   **`record` is a modifier** (boolean on `dial`/`voicemail`/`ai_agent`), *not* a node — avoids
   start/stop bracket pairs. **`play`** is its own node so the FL all-party consent notice
   (ARCHITECTURE decision 17) is an explicit, reusable step before a `dial`/`record`.
5. **Edges — embedded `next` map** keyed by port name (not a separate edge array). **Failure model:**
   explicit happy-path ports + a **flow-level `default_fallback`** (usually `voicemail`, else graceful
   `hangup`) so no call ever falls into dead air; any port can be explicitly wired to override, and a
   node may carry its own `fallback` to override the flow default.
6. **Form↔graph:** the **graph is the model**; the v1 rule-form is *one simplified emitter* of the same
   graph. Form-generated nodes carry an **`origin`** tag (`"form:voicemail"`) for round-tripping. A
   graph edited past what the form represents simply won't reopen in the form ("edit in builder").
   Authoring end-state resembles **Twilio Studio** (widgets = our nodes; widget transitions = our ports).
7. **Executor — in-memory interpreter per call** (holds the ARI WebSocket + channel handles + current
   node cursor). **One `call_event` per node transition** ("entered node N", "menu digit 2", "dial
   no_answer") — doubles as the audit trail and feeds ticket 05's data model. **No persisted DB
   cursor:** a telephony process restart drops the live RTP media anyway, so the call can't resume — the
   cursor would be effort for an unreachable case. Per-node hard timeout ceiling.
8. **Validation** runs on every `flow_version` write, **blocks activation** (not draft-save):
   - **Hard errors** (break the interpreter): (1) exactly one `entry`, graph root; (2) all `next`
     targets resolve to an existing node id in the same version; (5) a node's `next` keys ⊆ the ports
     its type declares.
   - **Warnings** (allow activation, surface in UI): (3) unwired non-failure port; (4) unreachable node
     (offer prune); (6) `ai_agent.agent_id` / `dial` targets exist at activation; (7) zero-cost cycle.
   - Cycles are legal (menu re-prompt loops back); per-node timeout/retry ceilings bound them, so
     infinite loops can't occur.

### Storage envelope (tables)
- **`flows`** — `id`, `name`, binding (→ number/assignment, ticket 07), `active_version_id`, `created_at`.
- **`flow_versions`** — `id`, `flow_id`, `version_number`, `graph jsonb`, `created_by`, `created_at`.
  **Immutable / append-only.**
- **`calls`** — stamps `flow_version_id` at `StasisStart`.

### `graph` jsonb shape
```json
{
  "schema_version": 1,
  "entry": "n_entry",
  "default_fallback": "n_vm",
  "nodes": {
    "n_entry": { "type": "entry", "next": { "default": "n_dial" } },
    "n_dial":  { "type": "dial",
                 "config": { "targets": ["+1..."], "strategy": "single", "timeout": 20 },
                 "record": true, "origin": "form:forward",
                 "next": { "answered": "n_hup" } },
    "n_vm":    { "type": "voicemail", "config": { "greeting": "...", "max_len": 120 },
                 "record": true, "next": { "recorded": "n_hup" } },
    "n_hup":   { "type": "hangup" }
  }
}
```
Nodes = object map keyed by id; edges = each node's `next` map keyed by port.

### Node/port contract (keeps the interpreter *total*)
| type | ports |
|---|---|
| `entry` | `default` |
| `play` | `default` |
| `hours` | `open`, `closed` |
| `menu` | `0`–`9`, `*`, `#`, `timeout`, `invalid` |
| `dial` | `answered`, `no_answer`, `busy_failed` |
| `voicemail` | `recorded`, `hangup` |
| `ai_agent` | `completed`, `transfer`, `failed` |
| `hangup` | *(terminal)* |

`config` blobs (indicative): `hours` = tz + weekly ranges + holidays; `menu` = prompt (TTS/audio) +
digit map + timeout + retries; `dial` = targets[] + strategy (single/ring-all/sequential) + per-target
timeout + caller-ID; `voicemail` = greeting + max length + beep; `ai_agent` = `agent_id` ref + entry
context (ticket 11); `play` = prompt.

### Worked example A — "Forward my cell, record, voicemail if no answer" (the 90% case)
`entry → dial(cell, record=true)`: `answered → hangup`; unwired `no_answer`/`busy_failed` fall to
`default_fallback = voicemail(record) → hangup`.

### Worked example B — "Consent notice → business hours → IVR → AI agent / sales; after-hours voicemail"
```
entry → play("This call may be recorded") → hours
  hours.open  → menu("1 sales, 2 assistant")
                  menu.1        → dial(sales, record) → answered → hangup ; (no_answer → fallback)
                  menu.2        → ai_agent(agent_X)  → completed → hangup
                                                       transfer  → dial(main)
                                                       failed    → voicemail
                  menu.timeout  → dial(main line, record)
                  menu.invalid  → dial(main line, record)
  hours.closed → voicemail(record) → hangup
default_fallback = voicemail(record) → hangup
```

### Downstream impact
- **Unblocks ticket 11** (AI-agent config/runtime): the `ai_agent` node + its `agent_id` ref and
  `completed`/`transfer`/`failed` ports are the seam ticket 11 designs against.
- **Feeds ticket 05** (data model): every node transition is a `call_event`; ticket 05 defines the
  ARI-event → `call_events` mapping this executor emits into.
- **Feeds ticket 10** (operator UX): the v1 rule-form is a simplified emitter of this graph; `origin`
  tags enable round-trip form editing.
- **Feeds ticket 07** (number lifecycle): `flows.binding` is where a number attaches to a flow.

### Fog graduated / recorded (schema supports, not built in v1)
Visual builder canvas as end-state authoring (Twilio-Studio-style); v1 rule-form as one emitter;
`condition` node (branch on new-vs-returning / blocklist); blind-`transfer` distinct from `dial`;
`queue`/hold; `goto`/subflow reuse; per-node recording-consent handling for FL all-party (partly
addressed by the `play` consent node).

### Post-close addendum — schema extension (surfaced by [ticket 13](13-in-platform-webrtc-calling.md), 2026-07-22)
The `dial` node's **target vocabulary is extended to accept operator endpoints** — an individual operator
(`operator:alice`) or an operator group (`operator:sales`) — alongside DIDs (`+1...`). This lets an inbound
flow "ring the operator's browser" (the WebRTC softphone leg, ticket 13) with no new node type: `dial`'s
existing single/ring-all/sequential strategies and `answered`/`no_answer`/`busy_failed` ports already cover
ringing one or several operators, with all-unavailable/no-answer falling through to `default_fallback`.
Presence is resolved at execution time (browser-registered AND app-level available toggle, ticket 13 Q6); an
unavailable operator target is treated as `no_answer`. No change to ports, storage, versioning, or the
interpreter — purely an additive `dial` target kind.

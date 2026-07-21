# Call-flow graph representation schema

Type: grilling
Status: open
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

## Answer

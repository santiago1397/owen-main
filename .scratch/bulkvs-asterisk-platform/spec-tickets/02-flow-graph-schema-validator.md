# 02 — Flow-graph schema + activation validator

**What to build:** An operator (via API) can create, version, and activate a call-flow graph, and the system refuses to activate a structurally invalid graph.

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

- [ ] Append-only `flows`/`flow_versions` with `graph jsonb`; each save creates a new immutable version
- [ ] Node vocabulary: entry/play/hours/menu/dial/voicemail/ai_agent/hangup; `record` is a modifier not a node; edges are each node's `next` map keyed by port
- [ ] A flow-level `default_fallback` (usually voicemail) catches unwired/errored ports
- [ ] Validation blocks activation on hard errors (one entry / resolvable targets / type-correct ports); warns (does not block) on unreachable/unwired/cycle
- [ ] Validation is a pure function over a graph and is unit-tested independently

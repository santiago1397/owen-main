# 06 — Operator platform UX shell (IA + Numbers hub + Calls sub-tabs)

**What to build:** The operator sees the new platform surface: a regrouped sidebar, a Numbers hub they manage from, and platform calls segregated from attribution calls -- all additive to the existing SPA.

**Blocked by:** 03, 04

**Status:** ready-for-agent

- [ ] Sidebar grouped into Attribution (existing, untouched), Platform (Numbers / Call Flows / Messages / AI Agents), System
- [ ] Numbers hub table (owner->media / flow / campaign / SMS-state columns) + per-number detail; Twilio/SignalWire rows read-only
- [ ] Calls page split into Attribution (Twilio/SignalWire) and Platform (BulkVS/Asterisk) sub-tabs over the same `calls` table by `provider_id`; existing `CallDrawer` unchanged
- [ ] Call Flows and AI Agents library pages exist as shells (deep authoring lands in later tickets)

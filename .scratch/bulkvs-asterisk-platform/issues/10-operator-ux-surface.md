# Operator UX/UI surface

Type: prototype
Status: open
Blocked by: 06, 07

## Question

What does the operator actually see and click — the "easiest UX, best of QUO/Twilio/SignalWire" ask made concrete?

- Information architecture: where do Numbers, Call flows, SMS conversations, AI agents, and the existing
  attribution/analytics dashboards sit relative to each other in one app?
- **Call-log tab segregation (user requirement, from [ticket 05](05-asterisk-as-provider-data-model.md)):** the
  existing **Twilio + SignalWire** call logs and the new **BulkVS/Asterisk** features live in **separate tabs**.
  Data-model side is settled (one `calls` table, filter by `provider_id`); this ticket decides the actual tab IA.
- The **per-number rule form** (from ticket 06's graph) — a rough clickable/stub layout to react to.
- The **SMS conversation** view (from ticket 08).
- How AI-agent config surfaces (from ticket 11) without cluttering the simple path.
- In-call operator UI (place/receive/transfer) coordinates with [ticket 13](13-in-platform-webrtc-calling.md).

Use `/prototype`; link the artifact from this ticket. Decide the IA + the rule-form layout; defer the visual
flow-builder canvas (fog).

## Answer

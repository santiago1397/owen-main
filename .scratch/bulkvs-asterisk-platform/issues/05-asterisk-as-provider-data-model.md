# Asterisk as a provider in the event-sourced data model

Type: grilling
Status: open
Blocked by: 02, 04

## Question

How does Asterisk-controlled telephony fit the existing `call_events` (truth) → `calls` (projection) model
without breaking the provider abstraction that Twilio/SignalWire use?

- Is Asterisk a new row in `providers`, and what is its `provider_call_sid` analogue (ARI `channel.id` /
  Stasis app id / Linkedid)?
- Which **ARI events** map to which `call_events` (StasisStart, ChannelStateChange, recording events,
  BridgeEnter, StasisEnd) and how do we preserve the atomic status-rank advance + dedup guarantees?
- Where do **recordings** produced by ARI land vs. the existing local-disk + transcription-gated retention path
  (reuse it)?
- How does **reconciliation** work without a Twilio-style REST "list calls" — from Asterisk CDR? ARI history?
- Do outbound + AI-agent calls use the same `calls` shape, and how are attribution/`campaign_id` stamped for
  numbers that now have live flows?

Use `/grilling` + `/domain-modeling`; record as an ADR-style decision.

## Answer

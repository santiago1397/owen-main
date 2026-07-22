# 04 — Asterisk provider ingestion (ARI-WS consumer)

**What to build:** A real inbound PSTN call to a BulkVS DID lands in OWEN as a normal call record with `provider=asterisk`, flowing through the same event-sourced projection as Twilio/SignalWire.

**Blocked by:** 01

**Status:** ready-for-agent

- [ ] A persistent ARI-WebSocket consumer in the single-replica worker feeds the existing `ingest_status_event` (no webhook; `verify_signature` is a no-op for this provider)
- [ ] One `calls` row per call keyed on `provider_call_sid = Linkedid` (legs collapse)
- [ ] `_ARI_TO_STATUS` maps channel lifecycle into the existing Twilio-CallStatus vocab, ranked off the entry channel
- [ ] Duplicate events deduped on `"{Linkedid}:{status}"`
- [ ] Asterisk registered as a 3rd provider; Twilio/SignalWire ingestion is unchanged; `_is_inbound` drop stays Twilio-only

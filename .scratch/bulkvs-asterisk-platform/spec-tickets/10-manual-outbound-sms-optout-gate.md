# 10 — Manual outbound SMS + opt-out + per-number gate

**What to build:** The operator replies to a thread from a 10DLC-enabled BulkVS number, opt-outs are honored, and outbound messages relay to GHL with an audit trail.

**Blocked by:** 09 External prerequisite: 10DLC brand + campaign registration (map ticket 12, HITL, awaiting account holder) must be live before outbound SMS is enabled on a number.

**Status:** ready-for-agent

- [ ] Outbound reuses `messages` (`direction='outbound'`) via a `message_send` worker job -> BulkVS `messageSend`, forward-only status; dedicated `POST /webhooks/bulkvs/message-status`
- [ ] Per-number gate `sms_enabled` + `sms_campaign_id` (manual entry; bridged from 10DLC registration)
- [ ] App-level opt-out `sms_opt_outs` per (number, contact) handling STOP/START/HELP
- [ ] Outbound relays to GHL; `sent_by_user_id` recorded for audit

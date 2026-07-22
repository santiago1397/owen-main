# 14 — Manual operator outbound calling

**What to build:** The operator places an outbound call from an owned BulkVS number in one click from a caller/contact/missed-call record, with a consent notice played and soft guardrails.

**Blocked by:** 13

**Status:** ready-for-agent

- [ ] Operator dials -> SIP.js INVITEs Stasis -> backend ARI originates the BulkVS trunk leg + bridges -> one `calls` row (`direction='outbound'`, `provider_id=asterisk`, `campaign_id` via from-number); no new schema
- [ ] From-number picker + remembered default; CLI must be an owned BulkVS DID (foreign/spoofed out of scope)
- [ ] Recording on by default with a pre-bridge ARI `play` consent notice to the callee
- [ ] Soft non-blocking guardrails only (warn on `sms_opt_outs` hit / outside 8am-9pm callee-TZ; operator may proceed)
- [ ] Entry points: the softphone dialer + a 'call' action on caller/contact/missed-call records

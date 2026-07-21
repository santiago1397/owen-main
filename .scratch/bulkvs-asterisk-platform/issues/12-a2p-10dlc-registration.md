# A2P 10DLC brand + campaign registration

Type: task
Status: open
Blocked by: 01

## Question

Not a decision — a prerequisite with a long lead time, surfaced by ticket 01. **Outbound SMS to US mobile numbers
is 100% blocked unless the sending numbers are registered under an A2P 10DLC brand + campaign** (via The Campaign
Registry, initiated through BulkVS as CSP). Brand approval ~1–3 business days; campaign ~3–15 business days. So this
must start early or it becomes the critical path for the SMS-send half of ticket 08.

Work to do (HITL — needs the account holder):
- Register the **Brand** in the BulkVS portal (EIN/business details).
- Register one or more **Campaigns** matching the actual messaging use-case (use-case type, sample messages,
  opt-in flow). Confirm BulkVS-specific fees + the `TCR` fields on `POST /tnRecord` that associate a DID→campaign.
- Record: brand/campaign IDs, which numbers are associated, approval dates, per-message throughput granted.

Inbound SMS and voice do **not** depend on this — only outbound A2P messaging does.

## Answer

<!-- record brand/campaign IDs, associated numbers, approval dates, throughput, BulkVS fees -->

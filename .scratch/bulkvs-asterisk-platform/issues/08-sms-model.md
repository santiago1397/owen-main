# SMS model — send / receive / threads / compliance

Type: grilling
Status: open
Blocked by: 01

## Question

Define the SMS (and MMS?) subsystem on BulkVS messaging.

- **Inbound:** BulkVS delivers inbound messages how (webhook)? New public signed endpoint under `/webhooks/*`,
  event-sourced like calls? New `messages` + `message_threads` tables?
- **Outbound:** send API integration, delivery-receipt handling, retries via the existing Postgres job queue.
- **Threading/UX:** conversation view keyed by (our number, contact) — reuse the `callers` identity? Tie SMS to
  the same campaign/attribution + GHL relay as calls?
- **Compliance:** A2P 10DLC brand/campaign registration — is it a prerequisite blocker for sending, and whose task?
- **MMS:** in for v1 or fog?

Use `/grilling` + `/domain-modeling`.

## Answer

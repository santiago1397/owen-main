# BulkVS platform + API capabilities

Type: research
Status: open
Blocked by: —

## Question

What does BulkVS actually give us to build on? Surface the facts every later decision waits on:

- **DID inventory / provisioning API** — endpoints to list owned numbers, search/order/release DIDs, and
  set per-DID routing/trunk. Auth model (API key/basic). Is there a webhook for inventory changes?
- **SIP trunk** — how inbound calls to a BulkVS DID reach our server: trunk auth (IP-ACL vs registration),
  BulkVS signaling/media IP ranges to allow-list, supported codecs (G.711/opus?), DTMF mode, TLS/SRTP support,
  concurrent-channel limits.
- **Outbound** — how we originate calls out through BulkVS (termination), caller-ID rules, any per-call API.
- **Messaging** — SMS **and** MMS: send API, inbound delivery mechanism (webhook payload shape, delivery
  receipts), throughput limits, and **A2P 10DLC / brand+campaign registration** requirements/process.
- **CDR** — does BulkVS expose call detail records / usage via API or portal export? (Relevant to reconciliation.)
- **Existing scaffolding** — the repo already has a provider-agnostic `sync-numbers` CLI (imports Twilio inventory).
  Note what a BulkVS adapter would need to fit it.

## Findings

<!-- resolved by /research subagent; link the captured research file here -->

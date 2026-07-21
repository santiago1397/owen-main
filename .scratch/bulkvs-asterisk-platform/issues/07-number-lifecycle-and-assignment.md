# Number lifecycle — BulkVS sync + assignment

Type: grilling
Status: open
Blocked by: 01

## Question

How do numbers flow from "bought on BulkVS" to "doing something in our system"?

- **Sync:** how the existing provider-agnostic `sync-numbers` CLI gets a BulkVS adapter — pull inventory,
  reconcile adds/removals, one-way (BulkVS is source of truth for ownership) vs. any write-back.
- **Assignment:** a number binds to a campaign (existing attribution model) **and** to a call-flow and/or an
  AI agent — what's the ownership model and the UI affordance? One flow per number? Shared flows reused across numbers?
- **Trunk routing:** does assigning a flow require any BulkVS-side routing change, or does everything route to
  Asterisk and branch internally by dialed DID? (Depends on ticket 01/04 findings.)
- Lifecycle states (available / assigned / released) and what release does to history.

Use `/grilling` + `/domain-modeling`.

## Answer

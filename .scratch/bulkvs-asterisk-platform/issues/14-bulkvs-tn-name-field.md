# BulkVS TN name/label field — mirror + write-back feasibility

Type: research
Status: open
Blocked by: —

## Question

Surfaced resolving [ticket 07](07-number-lifecycle-and-assignment.md): the number-label design assumes OWEN can
**mirror** each DID's name from BulkVS one-way (populate `friendly_name` from the `/tnRecord` response), with
optional future **two-way write-back** (edit label in OWEN → push to BulkVS). Both assumptions need confirming
against the BulkVS API — this is the one open fact behind ticket 07's label decision.

- Does a BulkVS `/tnRecord` **GET/list** response include a per-TN human name/description/label field (analogue of
  Twilio `friendly_name`)? Exact field name(s) and semantics.
- Does the `/tnRecord` **POST/PUT** (the route-update call from ticket 01) accept setting that name/description
  field — i.e. is **write-back** possible via API, or portal-only?
- If **no** name field exists at all: ticket 07 must switch from "mirror from BulkVS" to an **OWEN-owned label
  column** on `numbers` (sync never overwrites it). Flag which way it lands.

Resolve via `/research` against the BulkVS API docs / portal (some fields are GATED behind the login — mark
confidence, and note anything only confirmable inside the account). Record the field name(s) + read/write support
in the Answer block; ticket 07's §2 (label) and any future write-back work depend on it.

## Answer

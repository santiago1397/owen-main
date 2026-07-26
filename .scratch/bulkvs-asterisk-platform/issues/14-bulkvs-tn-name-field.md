# BulkVS TN name/label field — mirror + write-back feasibility

Type: research
Status: resolved (2026-07-22 — /research vs BulkVS OpenAPI spec)
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

**Resolved against BulkVS's own OpenAPI spec** (`https://portal.bulkvs.com/api/v1.0/openapi`, publicly
fetchable without login → treated [HIGH]/primary).

- **The field exists — `ReferenceID`**, documented as *"User inserted Note"*: a free-text, user-supplied
  per-TN label. This is the direct analogue of Twilio's `friendly_name`. [HIGH]
  - (Not to be confused with `Lidb` = outbound CallerID/CNAM name, which is broadcast on calls, not a private
    label. `ReferenceID` is the right friendly-name analogue.)
- **Read (GET `/tnRecord`): YES** — `ReferenceID` is returned in the list/record response. [HIGH]
- **Write (POST `/tnRecord`): YES** — `ReferenceID` is accepted in the request body alongside routing fields
  (`Trunk Group`, `Custom URI`, `Call Forward`, `PSTN Failover`) and messaging toggles. **Write-back is fully
  API-supported, not portal-only.** [HIGH]
- **[GATED]:** exact character limit / validation rules on `ReferenceID` — the human HTML docs page is
  login-gated; not publicly documented.

**Conclusion for [ticket 07](07-number-lifecycle-and-assignment.md) §2:** **Mirror from BulkVS** — round-trip
`ReferenceID` ↔ OWEN's `friendly_name`. No separate OWEN-owned label column is required. And because `ReferenceID`
is writable, the deferred **two-way write-back** (edit label in OWEN → push to BulkVS) is **confirmed feasible**
via the same POST — it's now a build choice, not a research unknown. (Optional: keep a local column only if OWEN
ever needs richer/longer metadata than the BulkVS "Note" field tolerates.)

Source: https://portal.bulkvs.com/api/v1.0/openapi

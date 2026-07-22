# Number lifecycle — BulkVS sync + assignment

Type: grilling
Status: resolved (2026-07-22 — grilling)
Assignee: svillahermosa
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

**How a number flows from "bought on BulkVS" to "doing something in our system."** All additive; Twilio numbers
keep `owner==media==twilio` and the existing `sync-numbers`/attribution/ingestion paths stay untouched.

### 1. Number identity — split ownership from media
Stop conflating the single `numbers.provider_id`. A number carries two provider facts:
- **owner_provider** — who owns the DID (inventory/buy/release/routing API) → `bulkvs` (or `twilio` legacy).
- **media_provider** — what carries/controls the media → `asterisk` (or `twilio` legacy).

Consistency with [ticket 05](05-asterisk-as-provider-data-model.md): a call on a BulkVS DID gets
`calls.provider_id = "asterisk"` (05's decision), which equals the number's **media_provider** — so attribution
resolves the Number by `(media_provider, to_number)`. **owner_provider is a number-only fact** (inventory/routing),
never stamped on calls. Exact column shape (two string cols vs `provider_id` + `owner_provider_id`) → ticket 05 to
formalize; the *distinction* is the decision here.

### 2. Sync — one-way mirror; buy & release happen in the BulkVS portal
- **In-app buying is OUT OF SCOPE** — operator buys/releases DIDs in the BulkVS portal. `/orderTn`+`/exchanges`
  unused for now. OWEN's job is to auto-list what's owned and let the operator label + assign it.
- `sync-numbers` gains a **BulkVS adapter** in `_number_sources()` that **polls `/tnRecord`** (no webhook),
  upserting keyed on `(owner_provider, phone_number)` with `media_provider="asterisk"`, `active=true`. Same
  add-only idempotency as today; never touches `campaign_id`/`flow_id`.
- **Label = one-way mirror from BulkVS** for now → populate the existing `friendly_name` from the TN record.
  Two-way write-back (edit in OWEN → push to BulkVS) is deferred and gated on a fact we haven't confirmed → new
  **[ticket 14](14-bulkvs-tn-name-field.md)** (does `/tnRecord` return/accept a per-TN name field at all? If not,
  OWEN needs its own operator-owned label column instead of mirroring).
- **Release:** a DID that vanishes from `/tnRecord` inventory (released in the portal) → sync **soft-releases** it:
  `active=false`, set `released_at`, freeze all history. Never hard-delete, never recycle (ARCHITECTURE #2).
- **Re-appearance:** a re-bought DID reappearing in inventory **reactivates the same row** (clear `released_at`),
  preserving its old history — no duplicate row.

### 3. Trunk routing — every DID → Asterisk, branch by dialed DID
Routing is set **once at provision time** via BulkVS `/tnRecord` route → your SIP trunk group → Asterisk. Assigning
or changing a flow later needs **no BulkVS-side change**: every DID lands on Asterisk, whose Stasis app branches by
**dialed DID** (`+E.164` RURI, per [ticket 04](04-prove-one-real-call.md)) to that number's flow.

### 4. Assignment — number → one shared flow, plus campaign for attribution
- A number carries `campaign_id` (existing attribution, unchanged) **and** a new **`flow_id`** FK → a shared,
  first-class **flow** entity (schema = [ticket 06](06-call-flow-graph-schema.md)).
- **Flows are reusable across numbers** (many numbers → one flow); edit a flow once and every number on it updates.
  Not one-hard-wired-flow-per-number.
- **AI agents are never bound directly to a number** — an agent is a node *inside* the flow-graph (ticket 06/11).
  A "pure AI number" is just a trivial flow `answer → AI-agent node`. One execution path, no competing handlers.
- **No-flow fallback ladder** (a synced number is never a silent dead-end):
  1. `flow_id` set → run the flow.
  2. else legacy `forwards_to` set → forward (preserves today's behavior).
  3. else → **capture the call in OWEN** (attribution + history + recording fire normally), log it as a **missed
     call**, and allow the operator to **answer it in-app** (WebRTC → [ticket 13](13-in-platform-webrtc-calling.md)).

### 5. Lifecycle states — derived, not a stored enum
No `status` column (it drifts vs the FKs it summarizes). State is **derived** from `active` + `released_at` +
`flow_id`/`campaign_id`:
- **available** = active, no `flow_id` and no `campaign_id`.
- **assigned** = active with a `flow_id` *or* `campaign_id` (either — a number can attribute to a campaign while
  still sitting on the missed-call default because it has no flow yet).
- **released** = `active=false`, `released_at` set, history frozen.

### New `numbers` columns for ticket 05/06 to formalize (additive, non-breaking)
`owner_provider` + `media_provider` (§1), `flow_id` FK → flows (§4), `released_at` timestamptz nullable (§5).
`friendly_name` reused as the mirrored label (§2, pending ticket 14).

### Surfaced / graduated
- **[Ticket 14](14-bulkvs-tn-name-field.md)** (research): does BulkVS `/tnRecord` return and/or accept a per-TN
  name/description field — determines label mirror vs OWEN-owned label, and whether two-way write-back is possible.
- In-app number **buying** ruled **out of scope**.
- In-app **answer of missed/unassigned calls** leans on **ticket 13**.

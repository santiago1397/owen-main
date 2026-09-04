# 03 — BulkVS number sync + lifecycle

**What to build:** The operator's BulkVS DIDs appear in OWEN automatically and stay in sync with the BulkVS portal, with correct labels and derived lifecycle state, without any manual number entry.

**Blocked by:** None — can start immediately

**Status:** ready-for-agent

- [ ] `sync-numbers` adapter polls `/tnRecord` (add-only); no webhook
- [ ] `numbers` gains `owner_provider` (bulkvs) vs `media_provider` (asterisk); attribution resolves by `(media_provider, to_number)`
- [ ] A vanished DID soft-releases (`active=false` + `released_at`, history frozen); a re-bought DID reactivates the same row
- [ ] Label one-way mirrored from BulkVS `ReferenceID` -> `friendly_name`
- [ ] Lifecycle (available/assigned/released) is derived from `active` + `released_at` + `flow_id`/`campaign_id`; no status enum

# 09 — BulkVS SMS inbound + Messages inbox

**What to build:** A text to a BulkVS number appears in a two-pane Messages inbox threaded by conversation, and relays to GHL -- reusing the existing inbound SMS subsystem.

**Blocked by:** 03, 06

**Status:** ready-for-agent

- [ ] BulkVS SMS adapter verifies inbound webhooks by IP allow-list via an additive `_verified()` extension (flagged shared-code change); synthetic `sha256(from|to|body|ts)` SID
- [ ] Inbound messages reuse the existing `messages` table + `parse_message_event` + GHL relay
- [ ] Threads derived by `(number_id, caller_id)` + `last_read_at`
- [ ] Two-pane inbox UI, polling (no websocket); composer disabled on non-10DLC numbers

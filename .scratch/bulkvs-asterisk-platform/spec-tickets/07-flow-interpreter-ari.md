# 07 — Flow interpreter on ARI

**What to build:** Calling a DID with an assigned flow runs it live: the caller hears the greeting, IVR routing works, calls forward, and voicemail catches the rest -- never dead air.

**Blocked by:** 02, 03, 04

**Status:** ready-for-agent

- [ ] In-memory ARI interpreter executes a flow-version: entry/play/hours/menu/dial/voicemail/hangup + `record` modifier
- [ ] Flow-version is pinned at `StasisStart` (like `campaign_id` at ingest)
- [ ] One `call_event` emitted per node transition; no persisted cursor
- [ ] Unwired/errored ports fall through to `default_fallback`
- [ ] `dial` supports a number target now; operator-target kind reserved for the softphone ticket

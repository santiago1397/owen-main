# 05 — Asterisk recordings reuse + CDR reconcile

**What to build:** An Asterisk call that records produces a WAV and transcript through the existing pipeline, and no call is lost if the worker restarts mid-call.

**Blocked by:** 04

**Status:** ready-for-agent

- [ ] Bridge WAV moves into the existing recordings table + fetch/transcribe pipeline (no parallel recording system)
- [ ] Asterisk CDR is reconciled into Postgres, backfilling the same `call_events`->`calls` projection
- [ ] A worker killed mid-call still ends up with a complete, correctly-projected `calls` row after reconcile

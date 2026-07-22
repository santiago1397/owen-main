# 08 — Rule-form flow authoring UI

**What to build:** The operator authors a call flow through a simple linear form (no graph editor), saves it, and it runs on the next call.

**Blocked by:** 02, 06

**Status:** ready-for-agent

- [ ] Linear 5-section form: hours -> greeting + record-modifier -> IVR menu -> default routing -> fallback
- [ ] The form is the simplified graph emitter (`origin`-tagged for round-trip); save creates a new append-only flow version
- [ ] Validate runs the ticket-02 checks and surfaces hard errors vs warnings
- [ ] Visual builder is a disabled 'later' tab (out of scope here)

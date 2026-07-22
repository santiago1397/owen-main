# 11 — AI agent config + library + VoiceAgentSession seam (dummy)

**What to build:** The operator creates a reusable AI voice agent, drops it into a flow node, and a call routes through the node and exits by port -- proven end-to-end with a dummy engine before real audio.

**Blocked by:** 06, 07

**Status:** ready-for-agent

- [ ] Versioned `agents`/`agent_versions` objects (persona/voice/greeting/model/tools[]/knowledge/guardrails); `agent_version_id` pinned on node entry
- [ ] AI Agents library UI; an agent is picked from a dropdown inside a flow node, never bound to a number
- [ ] `ai_agent` node wired into the interpreter: session returns `{port,data}`, interpreter drives the edge; the agent never bridges
- [ ] `VoiceAgentSession` seam mirrors `TranscriptionEngine` (Protocol + registry + per-agent engine; global `VOICE_AGENT_ENGINE` kill-switch); `dummy` engine implemented, others stubbed

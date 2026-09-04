# Customer context at call start — agreed design

> Outcome of a design interview (2026-09-04). Sixteen decisions, each explicitly chosen.
> **Nothing here is built yet.** Companion: [`AI_AGENT_SPEC.md`](AI_AGENT_SPEC.md) (the agent
> platform this plugs into), [`GHL_SYNC_SPEC.md`](GHL_SYNC_SPEC.md) (GHL API behaviour).

## The question this answers

> "If I want to integrate another CRM or system, where it has all the information or current
> state of a customer just by passing information to the start context when we receive a call
> and recognise the number — is that possible?"

Yes. And the design is deliberately **not GHL-shaped**, because a future in-house CRM was
named as a real possibility rather than a hypothetical.

## What already exists (verified in code, 2026-09-04)

| Piece | Status |
|---|---|
| Agent knows the caller at session start | ✅ `SessionIn.caller_number` |
| A place to inject context | ✅ `AgentConfig.knowledge` is concatenated into the system prompt |
| Agent can fetch mid-call | ✅ Custom HTTP tools (D6) — **the pull half works today, no new code** |
| Caller history | ✅ `callers` (31,663 calls) + `call_captures` + `/api/agent-runtime/calls/{linkedid}` |
| Phone → customer lookup | ❌ `ghl_api.py` has upsert/opportunity/note — **no lookup by phone** |
| Call → CRM timeline | ⚠️ `handle_call_relay_ghl` is **built and has never run**: `GHL_CALL_WEBHOOK_URL` is empty |

**Two findings that shape the design.** The call relay already assembles direction, status,
missed-flag, full transcript and analysis — it is switched off, not missing. And it targets
the **webhook** path, while the email relay was deliberately migrated to the direct v2 API to
avoid GoHighLevel's per-execution premium charge. Switching the call relay on as-is would pay
that premium on every completed call.

---

## C1 — Push a thin context blob, and offer a pull tool

Push identity plus open state (under ~200 words) into the system prompt before the agent
speaks; leave detail to a tool the agent calls when it needs it.

Push is the entire value — a greeting that already knows the name is worth more than anything
fetched later. But pushing everything pays tokens on every call for data most calls never
touch, and spends the latency budget. The pull half already exists as a custom HTTP tool.

## C2 — A generic provider contract, not a GHL integration

OWEN calls a **context provider**; GHL is one implementation. Three systems are already in
play (GHL, Workiz history, OWEN's own records) and an in-house CRM is anticipated. Hardcoding
GHL means writing this again for the second system.

## C3 — Identity: normalised match, OWEN first

Strip formatting, tolerate a missing country code, match on the last 10 digits. Resolve
against **OWEN first** — local, no latency, no failure mode, and 31,663 calls of history —
then the provider.

Fuzzy matching (spouse numbers, shared household lines) is rejected. **Greeting the wrong
person by name is far worse than not greeting them at all.**

## C4 — Allowlisted fields, declared on the agent version

The provider may return anything; OWEN passes through only declared keys. Logging records
**which fields were injected**, never their values.

Whatever is pushed lands in the model's context and in `transcriptions`. Without an allowlist
a caller's balance, address and notes end up in a transcript that `api/ai/content.py` hands to
any key holding the `content` scope. Field-level logging keeps it debuggable without
duplicating PII into a second store.

## C5 — Block, capped at ~1.2s, hidden inside the media wait

Fire the lookup at session start; await it just before speaking, ceiling ~1.2s. Past that,
speak the generic greeting and let context land for turn two.

`attach_media_to_call` already waits for Asterisk to dial back into the AudioSocket listener —
1 to 2 seconds in the live logs — and the greeting cannot be spoken before it. **A lookup
faster than that is free.** A CRM outage must never dead-air a caller.

## C6 — Split execution along the existing seam

**OWEN** assembles the local half (callers, prior captures, last call) — it has the database
and the query is free — and passes it plus the provider config in the session payload.
**owen-voice** calls the external provider concurrently with media attach and merges.

Same division D13 already settled: OWEN owns data, owen-voice owns the latency budget. Doing
the external call in OWEN would serialise it *before* `POST /sessions`, which is where it is
most expensive.

## C7 — Structured response, prose summary

```
{ "display_name": "Maria Santos",
  "summary": "3 open invoices. Last job Aug 12 (roof repair). Replacement quoted $14k.",
  "facts": { ...allowlisted keys... } }
```

The CRM describes its own state better than a renderer we would write; `facts` stays
allowlisted so the blob cannot grow unbounded.

## C8 — No cache

The entire value is *current* state. A cached "no open jobs" served to someone who booked one
four minutes ago is worse than a slower lookup. At a handful of concurrent calls there is
nothing worth saving. If a provider proves slow, fix it there rather than with staleness here.

## C9 — OWEN owns identity, the CRM owns state

`callers.label` is documented as a manual override, and the platform holds the line everywhere
that humans win over models — a person typed that name deliberately. OWEN knows nothing about
invoices or job status, so the CRM is authoritative there. Authority is per field, not per
system.

## C10 — Write-back post-call, through the job queue

Captures ride the existing relay rather than being written mid-call by the agent.

An agent writing to the CRM mid-call, on a mis-heard name, with no review, is how you get 200
junk contacts — and `WORKIZ_IMPORT.md` already records how painful GHL cleanup is (deleting a
contact cascades to its opportunities). The queue also supplies retry, backoff and
dead-lettering for free, and `call_captures` already carries a relay-once guard.

## C11 — The CRM gets a timeline entry, not a transcript

Per call: outcome, duration, what was captured, whether it transferred, and a **link back to
OWEN**. Not the full transcript.

The CRM should answer *"what happened with this customer?"* in five seconds. A 40-turn
transcript answers it in five minutes, and GHL has no transcript search worth using — it would
be a worse copy of something OWEN already stores with the audio beside it. **Full fidelity in
OWEN; timeline in the CRM.** The same split `GHL_SYNC_SPEC` settled for jobs.

## C12 — Direct API, never the webhook trigger

The dormant webhook path costs a GHL premium execution per completed call — thousands a month
at this volume — for something the existing Private Integration Token does free.
`ghl_api.add_contact_note` already exists. **Treat `GHL_CALL_WEBHOOK_URL` as dead.**

## C13 — Provider failure degrades loudly

Generic greeting, plus a WARNING into `app_logs` (surfacing via `/api/ai/errors?since=6h`) and
a counter on the session.

Refusing the call is clearly wrong. Silent degrade is the trap: the agent quietly stops
recognising anyone, every call still "works", and nobody notices for a week.

## C14 — Provider configured on the agent version

Pinned and activation-validated, beside the custom tools, transfer allowlist and field
allowlist. One place to look when an agent misbehaves, and *"which CRM did this call
consult?"* stays answerable from `calls.agent_version_id`. A specialist agent can point at a
different system without disturbing the others.

## C15 — One contract, two directions

```
POST <provider>/lookup  {caller_number, dialed_number, linkedid}
                     -> {display_name, summary, facts{...}}

POST <provider>/report  {linkedid, caller_number, outcome, duration_s,
                         captures[...], transfer, owen_url}
                     -> 200
```

Making only the read generic would leave a CRM-shaped hole in the middle of a design whose
point is not being CRM-shaped. Two endpoints is barely more work than one, and it turns "swap
the CRM" from a project into a config change.

**Consequence:** `call_captures.relayed_to_ghl` should be renamed — it is no longer
GHL-specific.

## C16 — Built-in adapters *and* a URL kind

A provider is configured as `kind: ghl` (built in, uses the token already in `.env.prod`) or
`kind: http, url: ...` (anything implementing C15).

Pure-URL is architecturally cleaner and rejected anyway: it would require deploying and
maintaining a translator service before this works on the CRM actually in use — a real cost
paid immediately for flexibility needed later. Adapters-only is how it ends up CRM-shaped
again, with every new system becoming a PR against OWEN. A future in-house CRM implements two
endpoints, and OWEN never learns anything about it.

---

## Not decided here

- **Whether an operator sees the pushed context** during a monitored call. Probably yes, but
  it is a UI question and nothing depends on it.
- **Rate limiting the provider.** No cache (C8) means one lookup per call; at 4 concurrent
  sessions that is not yet a load problem.
- **Multi-tenancy** — whose CRM, whose credentials — deferred with the rest of
  "other projects to OWEN" (D6).

## Build order, when it is picked up

1. Provider contract plus the `kind: http` adapter, configured on the agent version
   (C2/C14/C15/C16).
2. The local half in OWEN (`callers` + captures). Useful on its own, with no external
   dependency — an agent that knows "this caller has rung 4 times before" is already ahead.
3. Push into the system prompt with the capped wait (C5/C6/C7).
4. The `kind: ghl` adapter. `lookup` needs a phone search that `ghl_api.py` does not have yet.
5. The `report` direction, replacing the dormant webhook relay with the direct API (C11/C12).

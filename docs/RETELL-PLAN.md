# Retell voice agents — plan and cross-repo contract (2026-10-06)

The same file lives in both repositories (`ghl-clone/docs/RETELL-PLAN.md` and
`owen-main-software/docs/RETELL-PLAN.md`). If you change the contract in one, change it in
the other in the same piece of work.

## Decisions (owner, 2026-10-06 — amends the 2026-09-22 voice-agent decisions)

1. **Split of ownership.** Retell's dashboard owns HOW an agent talks: prompt, conversation
   flow, voice, knowledge base. The CRM owns everything business: which number goes to which
   agent, Answering on/off, which customer facts the agent may receive, the call log on the
   customer's thread. The CRM stores the Retell agent id and shows Retell's settings read-only.
   The customer is known BEFORE the agent says its first word.
2. **Call path:** BulkVS → Asterisk → owen-main's flow → Retell, by Retell's "dial to SIP URI"
   method (`POST /v2/register-phone-call`, then dial `sip:{call_id}@sip.retellai.com`). Flows,
   recording, voicemail fallback, Listen / Take over all stay in owen-main.
3. **Retell is one engine** (`engine: "retell"`) beside `owen_voice`, chosen per agent. Rollback
   is a setting. owen-voice is kept and keeps its tests.
4. **Tiered disclosure.** First name / "you have a job with us" may be used at once. Dates, Zuper
   status, technician and history only after the caller confirms the street address. NEVER
   prices, invoices, money, internal notes.
5. **"Registered"** = a CRM contact, OR a customer who exists only in Zuper (matched through
   `dispatch_jobs.phones`). Last ten digits, exactly one customer; anything else is unknown.
6. **Numbers are assigned to agents in the CRM** (AI Agents → Phone numbers, ADMIN), with a mode:
   `ai_first`, `staff_then_ai`, `after_hours_ai`. owen-main keeps and builds the flow.
7. **Context sources are switched per agent**, every one OFF by default.
8. **History = summaries that already exist** (Quo, Zuper Connect, earlier AI calls) + last 3
   texts. No new AI spend.
9. **The address IS sent to the agent** (owner's choice); the agent compares it itself and is
   told never to read it back. Consequence accepted: addresses sit in Retell's call logs.
10. **General FAQs live in Retell knowledge bases.**
11. **Spend cap is a setting**, pilot $25/day, alert at 80%, counted with Retell's real
    `call_cost.combined_cost`.
12. **Pilot:** the owner tests on a spare BulkVS DID first; real traffic is decided after.
13. **Tools:** transfer, end call, capture lead, and `request_change` (→ an urgent CRM task).
14. **Versions:** the CRM stores the Retell agent id; calls use Retell's latest published
    version; every call records the Retell version that answered.
15. **After a call** the CRM gets transcript, Retell's summary/outcome, owen-main's recording,
    captured lead, cost. Retell's own storage of the recording is turned off
    (`data_storage_setting` / opt-out) — owen-main records the bridge.
16. **One multilingual agent per number.**
17. **Retell failure → the flow's fallback (voicemail)** + an alert.
18. Numbers, context switches and the cap are ADMIN only in the CRM; calls follow thread rules.
19. A live AI call shows in the top bar AND as a desktop notification; Listen / Take over work.
20. Company-owned Retell account; the key ONLY in owen-main's env; every webhook and function
    call is signature-checked.

Safety, unchanged: nothing here sends a text; no test reaches Retell (a guard refuses
`api.retellai.com` in both repos); nothing is deployed or switched on by this work.

## Contract between the repos

### C1. Agent config (CRM version config → owen-main `agent_versions.config`)
CRM draft keys added for a voice agent: `engine` (`"retell"` or empty), `retell_agent_id`
(string), `context_sources` (object of booleans, all default false):
`crm_card`, `zuper_job`, `call_summaries`, `texts`, `ai_calls`, `address`.

`voice.to_owen` sends `engine`, `retell_agent_id`, `tools`, `transfer_targets`,
`context_provider`, `guardrails`. `context_sources` stays in the CRM (the CRM enforces them —
owen-main never decides what it may read). For `engine: "retell"`: persona / greeting / voice /
knowledge are NOT required (Retell owns them) and the 6,000-char knowledge rule does not apply.
Allowed tools for retell: `transfer_call`→`transfer`, `end_call`, `capture_lead`,
`request_change`→`request_change`. Still never `send_text`.

owen-main validation for engine `retell`: `retell_agent_id` required (non-empty, ≤ 100 chars);
tools ⊆ {transfer, end_call, capture_lead, request_change}.

### C2. Caller brief — `POST /api/agent-context` (CRM, events:write token)
Body: `{"caller_number": str, "agent_name": str?}` (still `extra="forbid"`).
- No `agent_name`, or one that is not a non-archived voice agent: today's answer, unchanged.
- With a known agent: today's keys appear only if `crm_card` is on; added keys appear ONLY when
  their source is on:
  - `zuper_job` → `{"job_number","board","status","status_since","technician","scheduled_start"}` or null
  - `call_summaries` → `recent_calls: [{"at","channel": "quo"|"zuper_connect","direction","summary"}]`
  - `ai_calls` → AI calls added to `recent_calls` with `channel: "ai"`
    (at most 3 items in `recent_calls` overall, newest first, last 90 days, each summary ≤ 400 chars)
  - `texts` → `recent_texts: [{"at","direction","text"}]` (last 3, each ≤ 200 chars, sent/received
    only, never internal notes)
  - `address` → `"address": "<street>, <city>"` (contact's, else the Zuper job's)
- `known: true` also for a Zuper-only customer: then `"source": "zuper"`, `contact` is built from
  the Zuper customer name, `opportunity`/`next_appointment` null. Otherwise `"source": "crm"`.
- Never: money, notes, Checklist, email, other customers. Pipelines hidden from the token owner
  stay hidden. Unknown stays exactly `{"known": false}`. Budget 0.8 s.

owen-main renders the brief into Retell dynamic variables (strings):
`customer_known` ("yes"/"no"), `customer_first_name`, `customer_brief` (a rendered block that
STARTS with the fixed disclosure rule of decision 4 and "never read the address aloud"),
`caller_number`, `dialed_number`. A Retell prompt uses `{{customer_brief}}`.

### C3. Change requests — `POST /api/agent-requests` (CRM, events:write token, in `EVENTS_WRITE_PATHS`)
Body `{"caller_number","agent_name","owen_call_id","kind": "reschedule"|"cancel"|"other","request": str ≤ 1000}`.
CRM: same matching rule as C2. Known CRM customer with an open card → an urgent task on that card,
attributed "AI: <agent>"; otherwise (Zuper-only or no open card) → a Dispatch item
(`kind "ai_change_request"`, bell rings). Unknown caller → 200 `{"created": false, "reason": ...}`.
Idempotent on (`owen_call_id`, `kind`, `request`). Writes nothing to Zuper, enqueues nothing that
texts. Response `{"created": bool, "where": "task"|"dispatch"|null}`.

### C4. After the call — the existing `POST /api/events` "ended" event
`ai_call` gains (free-form dict, merged as today): `engine: "retell"`, `retell_call_id`,
`retell_agent_version`, `summary`, `sentiment`, `successful`, `cost_cents`,
`disconnection_reason`, `requests: [...]`. The CRM shows the summary and cost on the call.

### C5. Numbers — owen-main `/api/crm-link/numbers` (CRM key)
- `GET` → `{"numbers": [{"id","e164","label","assignable": bool,"reason": str|null,
  "assignment": {"agent_name","mode","hours"}|null}]}`. Only Asterisk-media (BulkVS) numbers are
  assignable; others say why.
- `PUT /api/crm-link/numbers/{id}/assignment` body `{"agent_name","mode","hours"?}`. owen-main builds
  a CRM-managed flow for that number from a template and remembers the previous `flow_id`.
  `after_hours_ai` requires `hours` (`{"tz","days":{"mon":[["08:00","17:00"]],...}}`), no
  invented default. Refuses (409) to replace a hand-built flow unless `"replace": true`.
- `DELETE /api/crm-link/numbers/{id}/assignment` restores the previous flow.

### C6. Spend — owen-main `/api/crm-link/agent-spend` (CRM key)
`GET` → `{"daily_cap_usd","alert_pct","today_usd"}`; `PUT` `{"daily_cap_usd","alert_pct"}`.

### C7. Live calls
`/api/crm-link/live-calls` lists Retell calls in the same shape (+ `"engine": "retell"`).
Take over hangs up the Retell SIP leg and bridges the operator. The CRM adds a desktop notification.

## Phases
0. Account + key (owner); Asterisk PJSIP endpoint to `sip.retellai.com` (TCP, ulaw) and Retell's
   IPs allowed — written as operator steps, not applied; DECISIONS amendments; Retell test guards.
1. owen-main: engine, function + webhook endpoints, numbers / spend routes, live calls, fixes
   (`runtime.py` recording enqueue without `db`; transcript language hardcoded "en"; dialled
   number not passed to the context lookup).
2. CRM: Retell voice agent fields, context switches, wider brief, `agent-requests`, Phone numbers
   tab, spend setting, summary/cost on calls, desktop alert.
3. Owner's test on a spare DID (Suggest mode, Listen on).
4. Real traffic decided after review.

## Open
- The Quo overflow BulkVS DID and the live agent's name are not recorded anywhere.
- Latency to first word over the extra SIP hop is unmeasured.

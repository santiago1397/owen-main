# A2P 10DLC brand + campaign registration

Type: task
Status: in-progress
Assignee: svillahermosa
Blocked by: 01

## Question

Not a decision — a prerequisite with a long lead time, surfaced by ticket 01. **Outbound SMS to US mobile numbers
is 100% blocked unless the sending numbers are registered under an A2P 10DLC brand + campaign** (via The Campaign
Registry, initiated through BulkVS as CSP). Brand approval ~1–3 business days; campaign ~3–15 business days. So this
must start early or it becomes the critical path for the SMS-send half of ticket 08.

Work to do (HITL — needs the account holder):
- Register the **Brand** in the BulkVS portal (EIN/business details).
- Register one or more **Campaigns** matching the actual messaging use-case (use-case type, sample messages,
  opt-in flow). Confirm BulkVS-specific fees + the `TCR` fields on `POST /tnRecord` that associate a DID→campaign.
- Record: brand/campaign IDs, which numbers are associated, approval dates, per-message throughput granted.

Inbound SMS and voice do **not** depend on this — only outbound A2P messaging does.

## Prep (agent, 2026-07-22) — checklist sharpened; execution still HITL

This is a **HITL task**: brand+campaign registration requires the account holder (EIN + business
details, inside the login-gated BulkVS portal). An agent cannot execute it or invent the resulting IDs.
Below is the research to make the checklist precise; the ticket stays **open** until the human runs the
registration and records the results in the "Answer" block at the bottom.

Research prompt run against primary sources (TCR CSP manual, CTIA Messaging Principles, T-Mobile/AT&T
carrier tables via Twilio/Telnyx docs, BulkVS's own 10DLC page). Confidence markers: **[HIGH]** =
corroborated; **[MED]** = single/provider source; **[GATED]** = only confirmable inside the BulkVS account.

### Recommended registration shape for this business
- **Brand type:** Standard, `PRIVATE_PROFIT` (LLC/Inc), registered against the **EIN** — legal name +
  EIN + address must match IRS records **exactly** (mismatch is the #1 rejection cause). Sole-Proprietor
  path exists (no EIN) but caps at ~1,000 T-Mobile segs/day, ~15 AT&T SMS TPM, one campaign, tiny DID
  count — only a fallback if there's genuinely no EIN. [HIGH]
- **Campaign use-case:** appointment reminders / dispatch confirmations / service notifications to
  existing customers ⇒ **Customer Care** and/or **Account Notification** (declared use-cases get higher
  throughput than Mixed). Use **Mixed** if traffic ever spans promo + transactional. If monthly volume is
  low, **Low Volume Mixed** is cheapest ($1.50/mo) but locks to the lowest throughput tier and **cannot be
  raised by vetting** — pick it only if daily volume stays under ~2,000 T-Mobile segments. [HIGH]
- **Vetting:** optional external/secondary vetting (~$41.50) raises the Trust Score → higher daily caps.
  Skip it if Low-Volume/modest volume is acceptable; opt in only if you need >~2,000/day to T-Mobile. [MED]

### Execution checklist (account holder, in BulkVS portal)
1. **Register the Brand.** Provide: exact legal entity name, EIN + issuing country, entity type
   (`PRIVATE_PROFIT`), registered address, **live website URL**, business vertical, primary contact.
   TCR auto-verifies EIN + legal name + address against third-party DBs — verify these match IRS records
   before submitting. (2026 rule: EIN must be ≥15 days old.) [HIGH]
2. **Register the Campaign.** Pick the use-case above. Supply: opt-in/consent description + exact opt-in
   language (how customers consented — web form / text-in / IVR / point-of-sale), CTA with "Msg & data
   rates may apply" + message frequency + links to a **live Privacy Policy + Terms**, and **≥2 real sample
   messages** containing the actual brand name (NO `[company name]` placeholders, NO bit.ly links). Ensure
   **STOP/HELP** replies include the brand name. These are the top rejection causes. [HIGH]
3. **Confirm BulkVS-specific fees + flow [GATED].** BulkVS publicly lists only (page dated Oct-2021, may be
   stale): campaign **$50 activation + $15/mo**; T-Mobile per-msg surcharge registered SMS **$0.0030** /
   MMS **$0.0100** (unregistered $0.0040 / $0.013). BulkVS does **not** publish its TCR brand-registration
   fee or vetting fee — get these from the portal or a support ticket. Also confirm whether BulkVS
   requires self-registration at campaignregistry.com vs full CSP-managed submission (their page splits at
   ~50–100 msgs/day; low-volume can ride a shared "Bulk Solutions" campaign).
4. **Associate DIDs to the approved Campaign ID.** After approval, bind each sending DID to the Campaign ID.
   ⚠ Prior research's claim that `POST /tnRecord` "carries TCR fields" is **not publicly documented**
   [GATED] — confirm the exact field name(s) that attach a DID→Campaign in the portal API (likely under
   Messaging → Messaging Instructions). This is the concrete fact **ticket 08 (SMS-send)** needs.
5. **Record results** in the Answer block below.

### Reference facts feeding ticket 08 / infra
- **Industry fees [HIGH, Aug-2025 TCR pricing]:** TCR brand $4.50; external vetting ~$41.50; campaign
  verify $15 one-time; monthly by use-case — Account Notification/2FA **$2**, Low Volume Mixed **$1.50**,
  Standard/Mixed **$10**; T-Mobile one-time campaign ~$50; carrier per-msg surcharge ~$0.003–0.005.
- **Throughput [HIGH]:** T-Mobile daily cap is **per-EIN by Trust Score** — 16–25→2,000, 26–50→10,000,
  51–75→40,000, 76–100→200,000 segs/day (Sole-Prop 1,000). AT&T is **per-campaign TPM** by vetting class
  (Standard hi 4,500 SMS TPM … Low-Volume 75 … Sole-Prop 15). Over-cap = error 30023 (resets midnight PT).
- **Timelines [HIGH/MED]:** brand ~1–3 business days; external vetting +5–10 days; **campaign the long
  pole — AT&T human review 2–4 weeks**, realistic end-to-end 2–3 weeks (worst 4–6). Each rejection restarts
  the clock. Unregistered US-mobile outbound = 100% blocked since Feb 2025. **⇒ start this now to keep it
  off the SMS-send critical path.**

### Still GATED (confirm from inside the BulkVS account)
BulkVS's actual brand-registration + vetting fees; whether it auto-submits for vetting; the `POST /tnRecord`
DID→Campaign field schema; self-register vs CSP-managed threshold; Verizon-specific granted throughput.

## Answer

<!-- account holder: record here once registered —
     Brand ID: … | Campaign ID(s): … | use-case: … | associated DIDs: … |
     brand approval date: … | campaign approval date: … | Trust Score / throughput granted: … |
     BulkVS fees actually charged: … | tnRecord field(s) used to bind DID→campaign: … -->

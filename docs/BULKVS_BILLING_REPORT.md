# BulkVS Billing Report — OWEN

Every figure in the per-call table is **what BulkVS actually charged**, read from their
rated call-detail API (`GET /voice`) — not an estimate. The Twilio and SignalWire columns
are modelled by applying each carrier's published rates to these same calls.

**Period:** 2026-07-22 to 2026-07-28  
**Records:** 29 billable legs (9 inbound, 20 outbound)  
**Answered time:** 851 seconds  
**Total charged by BulkVS:** $0.04983

> **One call can be two charges.** When a flow forwards an inbound caller back out over the
> trunk, BulkVS bills the inbound leg *and* the outbound leg. Both appear below as separate
> rows, seconds apart. That is correct metering, not double-billing.

---

## 0. Account balance and where the money went

| | |
|---|---|
| Funded | **$25.00** |
| **Current balance** | **$24.76** |
| **Spent to date** | **$0.24** |
| Low-balance threshold | $10.00 |
| Billing mode | Prepaid (`Invoiced Billing: Disabled`) |

### Reconciliation — fully solved

| Item | Detail | Amount |
|---|---|---|
| Voice usage | 29 rated call records, itemised in §4 | **$0.04983** |
| DID setup | 1 x $0.05 — the *purchased* number only | **$0.05000** |
| DID monthly | 2 numbers x $0.06 | **$0.12000** |
| **CNAM dips** | **9 inbound calls x $0.002** | **$0.01800** |
| Port-in (LNP) | `+15618788090` — US ports are free | **$0.00000** |
| | **Total spent** | **$0.23783** |

$25.00 - $0.23783 = **$24.7622**, which displays as **$24.76**. Exact match.

**How this was established.** The balance is only shown to 2 decimals, so a single figure
cannot be inverted directly. Instead every plausible combination of BulkVS's published
charges (setup x0-2 at $0.05/$0.25, monthly x0-4, CNAM dips, LRN dips) was enumerated and
filtered to those that display as $24.76. Exactly **two** survived, differing only by LRN —
and `/accountDetail` reports `Lrn: Disabled`, which eliminates the second. The solution is
therefore unique among plausible combinations.

Three things this settles:

1. **The setup fee is $0.05, not $0.25.** BulkVS's own pricing page states $0.05 for US
   origination numbers. (A widely-cited third-party review says 25 cents; it does not fit
   the balance and appears to be outdated.)
2. **Only the purchased number paid it.** Two setup fees would put spend at $0.29, well past
   the $0.24 actually spent. Porting really is free.
3. **CNAM dips ARE billed** — see the correction below.

### Correction: CNAM is charged, just not inside the call record

An earlier version of this report stated CNAM was not billed, reasoning that each inbound
record's `amount` equals its minutes exactly. That observation is correct — but the
conclusion was wrong. **The dip is billed separately from the call record**, and it is the
only component that makes the balance reconcile.

It is not a rounding detail: **9 dips = $0.018, or 36% of all voice spend** on this account.
Because it is charged per *call* rather than per *minute*, its share grows as calls get
shorter — on a 6-second inbound call the dip costs 66x the minutes.

**How to confirm it independently:** take an inbound call and watch the balance. If it falls
by the call's `amount` **plus $0.002**, CNAM billing is proven directly rather than inferred.
CNAM can also be switched off per-number in the portal if the caller-name data is not worth
$0.002 per call.

### The two numbers were acquired differently

| Number | How acquired | Evidence | One-time cost |
|---|---|---|---|
| `+15618788090` | **Ported in from Twilio** | `/portTn` order 1912506, COMPLETE, RDD 27 Jul; losing carrier *Twilio International-10X/4* | **$0.00** — US ports are free |
| `+16452516222` | **Bought on the platform** | activated 21 Jul; no port order exists | **$0.05** setup |

Porting a number in cost nothing at all — worth remembering for future numbers, since
buying one costs $0.05 and porting one costs $0.00.

### Ruled out

| Hypothesis | Why it is wrong |
|---|---|
| Per-channel / trunk fees | **BulkVS does not charge for SIP trunk channels** — only minutes. Creating `vps-main-trunk` and provisioning MaxIn=10 cost nothing. |
| $0.25 setup fee | Would make spend $0.29 (one number) or $0.54 (two) — both exceed the $0.24 actually spent. |
| Setup fee on the ported number | Two setup fees do not fit the balance; porting is free. |
| LRN dips | `Lrn: Disabled` on the account. |
| 10DLC, E911, SMS/MMS | No records; services disabled. |

**Important limitation: BulkVS exposes no transaction or invoice endpoint.** `/accountDetail`
returns the current balance and nothing else — no deposits, no charge ledger, no statement.
All 16 API endpoints were checked. That is why the reconciliation above had to be solved by
elimination against the balance rather than simply read off a statement — and it is why the
CNAM charge was invisible until the arithmetic forced it.

A second limitation: **the API caps history at 31 days.** Anything older is invisible.
Both numbers activated 21 and 27 July, so the window likely covers the account's whole
life — but that cannot be proven from the API.

---

## 0b. What consumes money besides calls

Every chargeable category BulkVS offers, audited against the account:

| Category | Evidence | Cost |
|---|---|---|
| Voice calls | 29 rated records | **$0.04983** |
| DID monthly | 2 numbers x $0.06 | **$0.12 / month** |
| Port-in / LNP | 1 order COMPLETE (RDD 27 Jul) | **$0.00** — Tier 0 LNP fee is $0.00 |
| SMS | `/mdr?Type=sms` → NoRecordsFound | $0 |
| MMS | `/mdr?Type=mms` → NoRecordsFound | $0 |
| E911 calls | `/voice?Type=e911` → none | $0 |
| Toll-free (8xx / 8yy) | none | $0 |
| E911 provisioning | `/e911Record` empty; service Disabled | $0 |
| 10DLC brand / campaign | `/campaigns` empty | $0 |
| LRN dips | service Disabled | $0 |
| **CNAM dips** | **billed separately from the call record — see §0** | **$0.002 / inbound call** |

On an ongoing basis the things that cost money are: **calls**, **$0.12/month** in number
fees, and **$0.002 per inbound call** in CNAM dips. Everything else on the price sheet is
disabled, unused, or genuinely free. One-time acquisition costs are reconciled in §0.

Note that CNAM is charged per *call*, not per minute, so it behaves quite differently from
everything else here — on this account it is already **36% of voice spend**, and that share
rises as calls get shorter.

---

## 1. BulkVS rates that apply to these calls

| Service | Rate | Applies to |
|---|---|---|
| DID – US48, Tier 0 (inbound) | **$0.0003 / min** | all 9 inbound legs |
| Outbound Calling Domestic | **$0.004 / min** | 18 of 20 outbound legs |
| Outbound — undocumented rate | **$0.0099 / min** | 1 outbound leg — see §5 |
| DID monthly (Tier 0) | **$0.06 / month** | both numbers |
| DID setup, one-time | **$0.05** | charged once, on the *purchased* number — see §0 |
| CNAM lookup | **$0.002 / inbound call** | all 9 inbound calls — billed outside the call record |

**Billing increment: 6 seconds**, 6-second minimum. Verified against all 29 records —
every charge equals `ceil(seconds / 6) x 6 / 60 x rate`, exactly.

Items on your price sheet that are **not** billed as part of a call record:

| Service | Sheet rate | Status |
|---|---|---|
| CNAM lookup | $0.002 / dip | **Charged** — but outside the call record; see §0 |
| LRN lookup | $0.0001 | Disabled on the account |
| E911 | $0.49 / number / month | Disabled on the account |

---

## 2. Carrier comparison

| | BulkVS | Twilio | SignalWire |
|---|---|---|---|
| Inbound / min | **$0.0003** | $0.0085 | $0.0036 |
| Outbound / min | **$0.004** | $0.014 | $0.0075 |
| Number / month | **$0.06** | $1.15 | $0.20 |
| CNAM / inbound call | **$0.002** | $0.010 | not published |
| Port-in (LNP) fee | **$0.00** | n/a | n/a |
| DID setup, one-time | **$0.05** | n/a | n/a |
| Billing increment | **6 seconds** | 60 seconds | 60 seconds |

The increment matters as much as the rate. Your calls average 29 seconds, so
per-minute rounding inflates billable time far more here than it would for a business with
multi-minute calls.

**Billable time these same calls would produce:**

| Carrier | Billed seconds | vs BulkVS |
|---|---|---|
| BulkVS | 924 s | — |
| Twilio | 1,980 s | 2.14x |
| SignalWire | 1,980 s | 2.14x |

Same calls, same seconds of conversation — but 2.1x the billable time on a 60-second increment.

---

## 3. What these calls would have cost elsewhere

### Usage only — calls plus CNAM

| Carrier | Calls | CNAM (9 dips) | Usage total | vs BulkVS | You save |
|---|---|---|---|---|---|
| **BulkVS** | $0.04983 | $0.01800 | **$0.06783** | — | — |
| Twilio | $0.41250 | $0.09000 | $0.50250 | 7.4x | $0.43467 (86.5%) |
| SignalWire | $0.21240 | not published | $0.21240 | 3.1x | $0.14457 (68.1%) |

SignalWire does not publish a CNAM rate, so its column excludes one — its true usage cost
would be higher. Twilio's CNAM is **$0.01 per inbound call, 5x BulkVS's $0.002**.

### Usage plus 2 phone numbers (one month)

| Carrier | Usage | Numbers | Total | You save |
|---|---|---|---|---|
| **BulkVS** | $0.06783 | $0.12 | **$0.19** | — |
| Twilio | $0.50250 | $2.30 | $2.80 | $2.61 (93%) |
| SignalWire | $0.21240 | $0.40 | $0.61 | $0.42 (69%) |

At this volume the **monthly number fees dominate everything else**. Twilio's $1.15/number
is 19x BulkVS's $0.06, and on its own accounts for most of the gap.

### If volume grew 100x, same call mix

| Carrier | Usage x100 | Numbers | Total | You save |
|---|---|---|---|---|
| **BulkVS** | $6.78 | $0.12 | **$6.90** | — |
| Twilio | $50.25 | $2.30 | $52.55 | $45.65/mo |
| SignalWire | $21.24 | $0.40 | $21.64 | $14.74/mo |

---

## 4. Every charge, in detail

`Answered` is real talk time. `Billed` is what BulkVS charged for after rounding up to the
next 6-second increment. Times are US Eastern, as BulkVS reports them.

| # | When (ET) | Dir | From | To | Answered | Billed | Rate/min | Charged | Twilio | SignalWire |
|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|
| 1 | 2026-07-22 10:55:24 | in | +12178584185 | 16452516222 | 21s | 24s | $0.0003 | **$0.00012** | $0.00850 | $0.00360 |
| 2 | 2026-07-22 11:22:08 | out | 16452516222 | 12178584185 | 25s | 30s | $0.0040 | **$0.00200** | $0.01400 | $0.00750 |
| 3 | 2026-07-23 23:08:18 | out | +16452516222 | 19549147244 | 23s | 24s | $0.0040 | **$0.00160** | $0.01400 | $0.00750 |
| 4 | 2026-07-23 23:09:37 | out | +16452516222 | 19549147244 | 24s | 24s | $0.0040 | **$0.00160** | $0.01400 | $0.00750 |
| 5 | 2026-07-23 23:38:28 | out | +16452516222 | 19549147244 | 26s | 30s | $0.0040 | **$0.00200** | $0.01400 | $0.00750 |
| 6 | 2026-07-23 23:43:22 | out | +16452516222 | 19549147244 | 19s | 24s | $0.0040 | **$0.00160** | $0.01400 | $0.00750 |
| 7 | 2026-07-23 23:45:42 | in | +16452516222 | 16452516222 | 45s | 48s | $0.0003 | **$0.00024** | $0.00850 | $0.00360 |
| 8 | 2026-07-23 23:46:07 | out | +16452516222 | 19549147244 | 19s | 24s | $0.0040 | **$0.00160** | $0.01400 | $0.00750 |
| 9 | 2026-07-23 23:55:34 | out | +16452516222 | 19549147244 | 18s | 18s | $0.0040 | **$0.00120** | $0.01400 | $0.00750 |
| 10 | 2026-07-23 23:56:08 | out | +16452516222 | 19549147244 | 22s | 24s | $0.0040 | **$0.00160** | $0.01400 | $0.00750 |
| 11 | 2026-07-23 23:58:34 | in | +19549147244 | 16452516222 | 32s | 36s | $0.0003 | **$0.00018** | $0.00850 | $0.00360 |
| 12 | 2026-07-24 01:22:09 | out | +16452516222 | 19549147244 | 140s | 144s | $0.0040 | **$0.00960** | $0.04200 | $0.02250 |
| 13 | 2026-07-24 01:31:50 | out | +16452516222 | 19549147244 | 19s | 24s | $0.0040 | **$0.00160** | $0.01400 | $0.00750 |
| 14 | 2026-07-24 01:57:01 | out | +16452516222 | 19549147244 | 10s | 12s | $0.0040 | **$0.00080** | $0.01400 | $0.00750 |
| 15 | 2026-07-24 08:57:43 | out | +16452516222 | 19549147244 | 11s | 12s | $0.0040 | **$0.00080** | $0.01400 | $0.00750 |
| 16 | 2026-07-24 09:08:48 | out | +16452516222 | 19549147244 | 18s | 18s | $0.0040 | **$0.00120** | $0.01400 | $0.00750 |
| 17 | 2026-07-24 10:45:32 | in | +15616905516 | 16452516222 | 10s | 12s | $0.0003 | **$0.00006** | $0.00850 | $0.00360 |
| 18 | 2026-07-24 10:45:32 | out | +16452516222 | 19549147244 | 12s | 12s | $0.0040 | **$0.00080** | $0.01400 | $0.00750 |
| 19 | 2026-07-24 10:46:22 | out | +16452516222 | 19549147244 | 13s | 18s | $0.0040 | **$0.00120** | $0.01400 | $0.00750 |
| 20 | 2026-07-24 10:47:01 | out | +16452516222 | 15616905516 | 11s | 12s | $0.0099 ⚠ | **$0.00198** | $0.01400 | $0.00750 |
| 21 | 2026-07-28 11:59:50 | in | +2348154668630 | 15618788090 | 24s | 24s | $0.0003 | **$0.00012** | $0.00850 | $0.00360 |
| 22 | 2026-07-28 13:16:12 | in | +17622396502 | 15618788090 | 9s | 12s | $0.0003 | **$0.00006** | $0.00850 | $0.00360 |
| 23 | 2026-07-28 14:10:39 | out | +15618788090 | 19549147244 | 45s | 48s | $0.0040 | **$0.00320** | $0.01400 | $0.00750 |
| 24 | 2026-07-28 14:39:58 | in | +12178584185 | 15618788090 | 5s | 6s | $0.0003 | **$0.00003** | $0.00850 | $0.00360 |
| 25 | 2026-07-28 15:04:41 | in | +12178584185 | 15618788090 | 16s | 18s | $0.0003 | **$0.00009** | $0.00850 | $0.00360 |
| 26 | 2026-07-28 15:04:49 | out | +12178584185 | 19549147244 | 7s | 12s | $0.0040 | **$0.00080** | $0.01400 | $0.00750 |
| 27 | 2026-07-28 16:42:38 | in | +19549147244 | 15618788090 | 28s | 30s | $0.0003 | **$0.00015** | $0.00850 | $0.00360 |
| 28 | 2026-07-28 16:42:45 | out | +19549147244 | 18583794393 | 21s | 24s | $0.0040 | **$0.00160** | $0.01400 | $0.00750 |
| 29 | 2026-07-28 19:45:03 | out | +15618788090 | 19549147244 | 178s | 180s | $0.0040 | **$0.01200** | $0.04200 | $0.02250 |
| | | | | **totals** | **851s** | **924s** | | **$0.04983** | **$0.41250** | **$0.21240** |

---

## 5. The one charge not on your price sheet

```
2026-07-24 10:47:01   +16452516222 -> 15616905516
11s answered, billed 12s @ $0.0099/min = $0.00198
```

Your sheet lists exactly one outbound rate: Outbound Calling Domestic at $0.004/min. This
call billed at **$0.0099/min — 2.5x that**. The sheet's Outbound section was cut off in the
screenshot, so there are almost certainly further published outbound tiers.

Worth chasing, because of the leverage: that single **11-second call cost more than all 9**
**inbound calls put together** ($0.00198 vs $0.00105). If it represents a class of
destinations rather than a one-off, outbound will scale quite differently from $0.004/min.

---

## 6. Live verification test

A controlled test call was placed on 28 July to check the billing end to end. The expected
charge was calculated **before** dialling, then compared against what BulkVS actually did.

| Check | Expected | Actual | |
|---|---|---|---|
| Duration | ~180s | 178s | ok |
| Billed seconds | 180s (6s increment) | 180s | ok |
| Rate | $0.004/min | $0.004/min | ok |
| **Charge** | **$0.01200** | **$0.01200** | **exact match** |
| Balance | $24.77 → $24.76 | $24.76 | ok |

Three things this settled:

1. **The local Asterisk CDR is unreliable for the carrier leg.** Its two legs disagreed —
   the operator leg recorded 180s, the trunk-facing leg 30s. BulkVS billed 178s. Costing
   from Asterisk would have priced this call around $0.002 instead of $0.012, **six times
   too cheap**. This is the second independent confirmation that reading BulkVS's own rated
   records is the correct approach.
2. **Timestamps are US Eastern.** BulkVS stamped `19:45:03`; Asterisk recorded
   `23:45:03 UTC`. An exact UTC-4 match, confirmed on a fresh call.
3. **Posting lag is about 6–7 minutes.** The call ended 19:45 ET; the record appeared
   between 19:50 and 19:52. Since the billing job polls every 10 minutes, a call surfaces
   within roughly 15 minutes of hang-up — so a missing recent call is not a fault.

---

## Notes and caveats

- BulkVS figures are **actual charges** from their billing API. Nothing is estimated.
- Twilio and SignalWire figures are **modelled** from published list rates applied to these
  same calls, assuming the per-minute rounding both carriers use. A real invoice could
  differ with negotiated or volume pricing.
- The one call BulkVS rated at $0.0099/min is modelled at Twilio's and SignalWire's
  *standard* outbound rate, since their pricing for that destination is unknown. Both would
  likely charge a premium too, so their columns are if anything understated.
- SignalWire also charges roughly $0.0007/min for the leg between your PBX and their
  network. That is **excluded** here, so the SignalWire column is conservative — its true
  cost would be a little higher.
- Number-fee comparisons assume 2 numbers, matching your current inventory.
- **CNAM is included** in §0 and §3 at $0.002 per inbound call. It is NOT in the §4 per-call
  table, because BulkVS bills it outside the call record — so §4 sums to the voice total,
  not to total usage.
- Excluded throughout: E911 and LRN, both disabled on the account.
- Call durations are BulkVS's own; a forwarded call legitimately appears as two records.

Sources: BulkVS portal price sheet and `/voice` rated CDR; Twilio and SignalWire published
pricing pages.

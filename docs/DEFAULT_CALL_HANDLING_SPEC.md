# Default call handling for unassigned DIDs — spec (Ticket 18)

What happens when a **BulkVS/Asterisk** number is called and it has **no flow assigned**.

Before this ticket that case was a deliberate silent no-op (`runtime.run_flow_for_stasis`
returned immediately), so the caller got **dead air** — the channel sat in Stasis, unanswered,
until they gave up. The `FLOW_FALLBACK_FORWARD_NUMBER` safety net did *not* cover it: that only
fires for a DID that *is* flow-assigned whose flow fails to resolve.

Design decisions were resolved interactively on 2026-07-23. Anything not listed keeps its
current behavior. Assigning a real flow to a number **overrides** everything here.

---

## The behavior

```
inbound call → Asterisk Stasis → number has flow_id IS NULL
  1. answer the caller
  2. play the recording-consent notice to completion   (INBOUND_CONSENT_MEDIA)
  3. look up AVAILABLE operators
       = operator-<slug> PJSIP endpoints currently REGISTERED (ARI /endpoints state=online)
  ├─ ≥1 available → ring them ALL simultaneously (caller hears ringback)
  │     • operator popup shows WHO is calling and WHICH DID they dialed
  │     • FIRST to answer is bridged to the caller; all other legs are hung up
  │     • the bridge is recorded (INBOUND_RECORDING_ENABLED)
  │     • call ends when either side hangs up
  │     • nobody answers within OPERATOR_RING_TIMEOUT_SECONDS → voicemail
  └─ 0 available → voicemail immediately

voicemail = greeting (VOICEMAIL_GREETING, TTS) → beep → record until hangup or
            VOICEMAIL_MAX_SILENCE_SECONDS of silence, capped at VOICEMAIL_MAX_DURATION_SECONDS
            → hang up. The WAV rides the EXISTING recording→transcribe→analyze→GHL pipeline
            and surfaces in the Inbox thread (unread) + Calls with audio and transcript.
```

## Decisions (and why)

| # | Decision | Rationale |
|---|---|---|
| 1 | **Availability = SIP registration.** An operator is ringable iff their browser softphone is currently registered. | The InCallBar availability toggle already registers/unregisters the endpoint, so this is the real-time truth. No presence table, no heartbeat. "Logged into the web app but softphone closed" is *not* available — there'd be nowhere to send the audio. |
| 2 | **Ring all available at once**, first-to-answer wins, others stop ringing. | Fastest pickup, simplest mental model, matches Quo. Round-robin was rejected: slower to connect, needs ordering/per-operator timeouts, unnecessary for a small team. |
| 3 | **Popup shows enriched identity** — caller's contact name (fallback: formatted number) and the dialed DID's friendly name (fallback: number) — with Answer / Decline. | The data is already in `callers.label` / `numbers.friendly_name`. Decline drops only that operator; the rest keep ringing. |
| 4 | **No-answer → voicemail after 25s.** Same voicemail as the no-operators case. | ~5–6 rings: long enough to grab, short enough the caller doesn't give up. One destination keeps the model simple. |
| 5 | **One global voicemail greeting**, not per-number. | These are *unconfigured* numbers by definition; per-DID greetings would need new schema/UI and fight the "default before you've configured anything" premise. A number that needs a bespoke greeting should get a real flow. |
| 6 | **Reuse the existing recording pipeline**; no new notification channel. | A voicemail is just an inbound call with a recording. Inbox unread state + the existing GHL call relay are the alert. |
| 7 | **Record operator-answered calls, with a consent notice first.** | Florida is all-party consent (ARCHITECTURE.md #17) and the manual *outbound* path already does exactly this. Feeds the transcript/analysis the attribution product depends on. |
| 8 | **Fixed built-in default + global settings**, not an auto-seeded editable flow. | "Ring all *currently available* operators" is dynamic and can't be a static operator list in a saved graph. Knobs (timeout, greeting, consent) are env settings; bespoke behavior = build a real flow and assign it. |
| 9 | **Busy operator = unavailable. No call-waiting in v1.** | SIP.js `SimpleUser` handles one session, so a mid-call operator can't take a second popup. The caller still reaches voicemail, which lands in the Inbox. Call-waiting is its own feature. |

## Implementation map

| Piece | Where |
|---|---|
| Settings | `backend/app/core/config.py` (`NO_FLOW_RING_OPERATORS`, `OPERATOR_RING_TIMEOUT_SECONDS`, `INBOUND_CONSENT_MEDIA`, `INBOUND_RECORDING_ENABLED`, `VOICEMAIL_*`) |
| The default handler | `backend/app/flows/runtime.py` → `_handle_unassigned` (called when `assigned` is False) |
| Presence lookup | `AsteriskAriClient.available_operators` — ARI `GET /endpoints`, `operator-*` with state `online` |
| Ring-group + first-answer bridge | `AsteriskAriClient.ring_and_bridge` / `_await_first_answer`. Also now backs the flow `dial` **operator-target** node (`dial_operator`), which was previously originate-only and never bridged. |
| Real voicemail capture | `AsteriskAriClient.voicemail` / `_await_voicemail_end`; interpreter node `_h_voicemail` delegates to it (the old node started a recording then hung up immediately, capturing nothing) |
| Play-to-completion + ringback | `AsteriskAriClient.play_and_wait` (awaits `PlaybackFinished`), `ring_start` / `ring_stop` |
| WS correlation | `app/flows/dtmf.py` playback + recording registries, fed by `workers/asterisk_consumer.py` |
| Popup | `frontend/src/components/IncomingCallModal.tsx`, one app-wide softphone via `lib/softphoneContext.tsx` (mounted above `<Routes>` so a call survives navigation) |
| Enrichment API | `GET /api/telephony/incoming-context?caller=&dialed=` |

**How the popup learns "to what number":** each operator leg is originated with caller-ID
`"<dialed DID>" <caller number>`, so the browser's SIP `From` carries the caller in the URI user
and the dialed DID in the display name. No custom SIP headers needed.

**Why operator legs never start a second flow:** they are originated with `originator=<caller
channel>` (so they inherit the caller's Linkedid and are never the entry channel) *and* tagged
with the flow-dial markers (`appArgs` + channel-id prefix) as defense in depth.

## Not done / follow-ons

- Call-waiting (second line, hold-and-swap).
- Per-number voicemail greetings and per-number fallback-forward.
- Round-robin / skills-based routing.
- Ringback during the operator ring relies on ARI ring indication on an answered channel; if a
  deployment wants music-on-hold instead, add a MoH class and swap `ring_start`.

## Verification checklist (real call)

1. Operator logs in, toggles **Available** (softphone registers).
2. Call an unassigned BulkVS DID → consent notice plays → operator popup shows caller + DID.
3. Answer → two-way audio; recording lands, transcribes, analyzes; GHL relay fires.
4. Repeat with the operator **unavailable** → voicemail greeting + beep → leave a message →
   message appears in the Inbox thread with audio + transcript.
5. Repeat with the operator available but not answering → rolls to voicemail after ~25s.

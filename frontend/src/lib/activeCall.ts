// The one thing the softphone hook CANNOT know on its own: which Asterisk channel the current
// call is, plus the "line" (DID/from-number) it is on — the pieces the redesigned in-call panel
// needs to drive hold/blind-transfer over ARI and to label the call.
//
// WHY A MODULE, NOT SOFTPHONE STATE: SIP.js only ever sees the operator's OWN leg (an INVITE
// with the peer's number). The caller/callee CHANNEL id (== the call's Linkedid /
// provider_call_sid) lives server-side. We get it from two disjoint places, neither of which is
// the SIP.js session:
//   - OUTBOUND: api.outboundCall's response carries `callee_channel` (control.place_outbound_call
//     returns it). It resolves AFTER the operator-leg INVITE has already arrived, so it can't be
//     set from onCallReceived — the call site stamps it here when the POST resolves.
//   - INBOUND: no channel id on the INVITE at all; the panel best-effort correlates the peer
//     number against the in-progress platform calls list and stamps the match here.
// A subscribable module singleton (mirrors lib/outboundIntent + lib/dialer) lets any of those
// producers write and the panel read, with no prop threading through the app shell.
import { useSyncExternalStore } from "react";

export type CallDirection = "inbound" | "outbound";

export type ActiveCall = {
  // Caller/callee channel to hold or blind-transfer (the call's Linkedid). Null until resolved
  // (outbound: from the originate response; inbound: from peer-number correlation) — the panel
  // disables hold/transfer with a hint while it is null.
  channelId: string | null;
  // The DID this call is on: the dialed number for inbound, the from-number for outbound. Drives
  // the panel header's "line" label (resolved to a friendly name there).
  line: string | null;
  direction: CallDirection | null;
};

const EMPTY: ActiveCall = { channelId: null, line: null, direction: null };

let current: ActiveCall = EMPTY;
const listeners = new Set<() => void>();

function emit(): void {
  for (const fn of listeners) fn();
}

/** Merge a partial update (e.g. stamp the channel id once correlation succeeds). */
export function setActiveCall(patch: Partial<ActiveCall>): void {
  current = { ...current, ...patch };
  emit();
}

/** Reset to empty — called when the call ends so a stale channel id can't leak into the next. */
export function clearActiveCall(): void {
  if (current === EMPTY) return;
  current = EMPTY;
  emit();
}

export function getActiveCall(): ActiveCall {
  return current;
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** React binding: re-renders the panel whenever the active-call info changes. */
export function useActiveCall(): ActiveCall {
  return useSyncExternalStore(subscribe, getActiveCall);
}

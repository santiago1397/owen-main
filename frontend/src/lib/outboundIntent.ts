// "We are placing an outbound call" signal, shared between the API layer and the softphone.
//
// WHY THIS IS A MODULE, NOT COMPONENT STATE: a manual outbound call (Ticket 14) reaches this
// operator as an ordinary incoming INVITE — the backend rings our softphone first, then bridges
// it to the callee. So the browser cannot tell "my own outgoing call" from "someone is calling
// me" by looking at the INVITE: its caller-ID is the number WE dialed. The only reliable
// signal is that we just asked the backend to place a call.
//
// It lives at module scope, and `api.outboundCall` marks it, so EVERY caller is covered
// automatically. The first version of this hung off each call site and was silently missed by
// the InCallBar dialer, which still showed the "Incoming call" popup for the operator's own
// call. One choke point makes that class of bug impossible.

// How long after asking for a call an arriving INVITE is treated as our own outbound leg.
// Generous enough for trunk setup, short enough that a genuine inbound call minutes later is
// never auto-answered.
const TTL_MS = 45_000;

let markedAt = 0;

/** Called by api.outboundCall the moment an outbound call is requested. */
export function markOutboundIntent(): void {
  markedAt = Date.now();
}

/**
 * True iff an outbound call was requested within the TTL. SINGLE-SHOT: reading clears it, so
 * exactly one INVITE can ever be claimed per request and a stale flag can't swallow a real
 * incoming call.
 */
export function consumeOutboundIntent(): boolean {
  const at = markedAt;
  markedAt = 0;
  return at > 0 && Date.now() - at < TTL_MS;
}

/** Drop a pending intent — e.g. the outbound request failed, so no INVITE is coming. */
export function clearOutboundIntent(): void {
  markedAt = 0;
}

// Manual operator outbound dialer glue (Ticket 14).
//
// Two tiny cross-component concerns, kept out of the softphone hook:
//  - remembered default FROM-number: a lightweight per-browser preference (localStorage). No
//    backend schema — the picker just re-selects it next time. (Single operator per browser.)
//  - "call" action prefill: a caller / contact / missed-call record elsewhere requests a dial;
//    the dialer (in InCallBar on the Calls › Platform tab) picks it up. A window CustomEvent +
//    a localStorage handoff so the request survives the route change to /calls.

const LAST_FROM_KEY = "owen_last_from_number";
const PENDING_DIAL_KEY = "owen_pending_dial";
export const DIAL_EVENT = "owen:dial";

export function getLastFromNumber(): string | null {
  return localStorage.getItem(LAST_FROM_KEY);
}
export function setLastFromNumber(n: string) {
  localStorage.setItem(LAST_FROM_KEY, n);
}

// Request that the dialer be prefilled with `number` and focused. Stored so a route change to
// /calls doesn't drop it; the event lets an already-mounted dialer react immediately.
export function requestDial(number: string) {
  localStorage.setItem(PENDING_DIAL_KEY, number);
  window.dispatchEvent(new CustomEvent(DIAL_EVENT, { detail: number }));
}

// Consume a pending dial request (one-shot), or null if none.
export function takePendingDial(): string | null {
  const n = localStorage.getItem(PENDING_DIAL_KEY);
  if (n) localStorage.removeItem(PENDING_DIAL_KEY);
  return n;
}

// Non-consuming check (the Calls page uses it to open on the Platform tab; InCallBar consumes).
export function hasPendingDial(): boolean {
  return !!localStorage.getItem(PENDING_DIAL_KEY);
}

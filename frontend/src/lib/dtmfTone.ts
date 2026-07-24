// Local audible feedback for the in-call keypad. sendDtmf() transmits the tone down the SIP
// leg (RFC 2833) so the far-end IVR hears it, but plays NOTHING in the operator's own ear — so
// a press feels dead. This synthesizes the real DTMF dual-tone locally via Web Audio: the same
// two frequencies a phone plays, short and low-gain, purely as click feedback.
//
// Routed to the operator's chosen speaker when the browser supports HTMLMediaElement.setSinkId
// on an AudioContext destination; otherwise it just uses the default output. Best-effort — any
// audio failure is swallowed (feedback is a nicety, never allowed to break the call).

// Standard DTMF row (low) + column (high) frequencies, in Hz.
const DTMF: Record<string, [number, number]> = {
  "1": [697, 1209], "2": [697, 1336], "3": [697, 1477],
  "4": [770, 1209], "5": [770, 1336], "6": [770, 1477],
  "7": [852, 1209], "8": [852, 1336], "9": [852, 1477],
  "*": [941, 1209], "0": [941, 1336], "#": [941, 1477],
};

let ctx: AudioContext | null = null;

function audioContext(): AudioContext | null {
  try {
    const Ctor = (window.AudioContext || (window as any).webkitAudioContext) as
      | typeof AudioContext
      | undefined;
    if (!Ctor) return null;
    if (!ctx) ctx = new Ctor();
    // Autoplay policies suspend a context created before a user gesture; a keypad press IS a
    // gesture, so resume() here reliably unblocks it.
    if (ctx.state === "suspended") void ctx.resume();
    return ctx;
  } catch {
    return null;
  }
}

/** Play the DTMF dual-tone for `digit` as brief local feedback (~140ms). No-op if unknown. */
export function playDtmfTone(digit: string, durationMs = 140): void {
  const pair = DTMF[digit];
  if (!pair) return;
  const ac = audioContext();
  if (!ac) return;
  try {
    const now = ac.currentTime;
    const end = now + durationMs / 1000;
    // A shared gain node with a short attack/decay envelope so the tone doesn't click on/off.
    const gain = ac.createGain();
    gain.gain.setValueAtTime(0, now);
    gain.gain.linearRampToValueAtTime(0.14, now + 0.012);
    gain.gain.setValueAtTime(0.14, end - 0.02);
    gain.gain.linearRampToValueAtTime(0, end);
    gain.connect(ac.destination);
    for (const freq of pair) {
      const osc = ac.createOscillator();
      osc.type = "sine";
      osc.frequency.value = freq;
      osc.connect(gain);
      osc.start(now);
      osc.stop(end);
    }
  } catch {
    /* feedback only — never surface */
  }
}

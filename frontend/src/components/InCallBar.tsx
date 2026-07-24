// Idle softphone bar — the "start a call & be reachable" surface (Ticket 13/14).
//
// Since the redesign, everything that happens DURING a call — mute, hold, blind-transfer,
// keypad, audio-device settings, hang up — lives in the global draggable <InCallModal>. This bar
// keeps only the IDLE responsibilities that must be reachable when NO call is up:
//   - the availability toggle (registers/unregisters the softphone so inbound calls can reach
//     this operator; unavailable => the interpreter's operator-target dial finds the endpoint
//     offline and falls through to default_fallback), and
//   - the outbound dialer (from-number picker restricted to owned BulkVS DIDs + a callee input +
//     "Call", which asks the BACKEND to originate + play the pre-bridge consent + bridge).
import { useEffect, useState } from "react";
import { api } from "../api";
import {
  DIAL_EVENT,
  getLastFromNumber,
  setLastFromNumber,
  takePendingDial,
} from "../lib/dialer";
import { useSoftphoneContext } from "../lib/softphoneContext";

type FromNumber = { id: string; phone_number: string; friendly_name?: string | null };

// Outbound dialer (Ticket 14): from-number picker restricted to owned BulkVS DIDs (+ remembered
// default), a callee input, and a "Call" action that asks the BACKEND to originate + play the
// pre-bridge consent notice + bridge over ARI. Soft guardrail warnings are shown, never blocking.
function Dialer() {
  const [fromNumbers, setFromNumbers] = useState<FromNumber[]>([]);
  const [from, setFrom] = useState<string>("");
  const [callee, setCallee] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);

  // Load owned DIDs once; select the remembered default (else the first).
  useEffect(() => {
    api
      .outboundFromNumbers()
      .then((rows: FromNumber[]) => {
        setFromNumbers(rows);
        const last = getLastFromNumber();
        const pick = rows.find((r) => r.phone_number === last) || rows[0];
        if (pick) setFrom(pick.phone_number);
      })
      .catch(() => setFromNumbers([]));
  }, []);

  // A "call" action from a caller / contact / missed-call record prefills the callee.
  useEffect(() => {
    const pending = takePendingDial();
    if (pending) setCallee(pending);
    const onDial = (e: Event) => setCallee(String((e as CustomEvent).detail || ""));
    window.addEventListener(DIAL_EVENT, onDial);
    return () => window.removeEventListener(DIAL_EVENT, onDial);
  }, []);

  const place = async () => {
    if (!from) return setNote("No owned BulkVS DID available to call from.");
    if (!callee.trim()) return setNote("Enter a number to call.");
    setBusy(true);
    setNote(null);
    setWarnings([]);
    try {
      setLastFromNumber(from);
      const res = await api.outboundCall(callee.trim(), from);
      setWarnings(res?.warnings || []);
      setNote("Placing call — your softphone will ring to connect.");
    } catch (e: any) {
      setNote(`Error: ${String(e?.message || e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        <span className="muted">Call from:</span>
        <select value={from} onChange={(e) => setFrom(e.target.value)}>
          {fromNumbers.length === 0 && <option value="">no owned DIDs</option>}
          {fromNumbers.map((n) => (
            <option key={n.id} value={n.phone_number}>
              {n.phone_number}
              {n.friendly_name ? ` (${n.friendly_name})` : ""}
            </option>
          ))}
        </select>
        <input
          placeholder="number to call, +1305…"
          value={callee}
          onChange={(e) => setCallee(e.target.value)}
        />
        <button disabled={busy || !from} onClick={() => void place()}>
          Call
        </button>
      </div>
      {warnings.map((w, i) => (
        <div key={i} className="muted" style={{ color: "#b26a00" }}>
          ⚠ {w}
        </div>
      ))}
      {note && <div className="muted">{note}</div>}
    </div>
  );
}

// The idle bar: availability status + toggle, then the outbound dialer. During a call, the
// controls (mute/hold/transfer/keypad/hangup) are owned by the global <InCallModal>.
export default function InCallBar() {
  const { state, setAvailable } = useSoftphoneContext();

  const pill: Record<string, string> = {
    offline: "#555",
    registering: "#8a6d3b",
    available: "#2e7d32",
    ringing: "#b26a00",
    "in-call": "#1565c0",
  };

  return (
    <div className="card" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span className="badge" style={{ background: pill[state.status] || "#555", color: "#fff" }}>
          {state.status}
        </span>
        <label className="muted" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <input
            type="checkbox"
            checked={state.available}
            onChange={(e) => setAvailable(e.target.checked)}
          />
          Available
        </label>
        <div style={{ flex: 1 }} />
        {(state.status === "in-call" || state.status === "ringing") && (
          <span className="muted" style={{ fontSize: 12 }}>
            Call controls are in the in-call panel ↘
          </span>
        )}
      </div>

      {/* Ticket 14: manual operator outbound calling (from-number picker + consent + bridge). */}
      <Dialer />

      {state.error && <div className="muted">Softphone: {state.error}</div>}
    </div>
  );
}

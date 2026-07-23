// In-call bar (Ticket 13) — fills ticket-06's reserved slot on the Calls Platform tab.
//
// Answer / hangup drive the operator's own SIP.js leg. Hold / blind-transfer are driven by
// the BACKEND over ARI (never browser->ARI) and act on the CALLER channel — which in this
// codebase is keyed by the call's Linkedid (== provider_call_sid), so we pass that as the
// channel id. An availability toggle registers/unregisters the softphone (unavailable =>
// the interpreter's operator-target dial finds the endpoint offline => default_fallback).
import { useState } from "react";
import { api } from "../api";
import { useSoftphone } from "../lib/softphone";

type TransferKind = "did" | "operator" | "ai_agent";

// `channelId` is the caller channel to hold/transfer (the selected/active platform call's
// provider_call_sid). Optional: without one, hold/transfer are disabled with a hint.
export default function InCallBar({ channelId }: { channelId?: string }) {
  const { state, setAvailable, answer, hangup } = useSoftphone();
  const [busy, setBusy] = useState(false);
  const [held, setHeld] = useState(false);
  const [xferKind, setXferKind] = useState<TransferKind>("did");
  const [xferTarget, setXferTarget] = useState("");
  const [note, setNote] = useState<string | null>(null);

  const canControl = !!channelId && (state.status === "in-call" || state.status === "ringing");

  const guard = async (fn: () => Promise<any>, ok: string) => {
    setBusy(true);
    setNote(null);
    try {
      await fn();
      setNote(ok);
    } catch (e: any) {
      setNote(`Error: ${String(e?.message || e)}`);
    } finally {
      setBusy(false);
    }
  };

  const toggleHold = () =>
    guard(async () => {
      await api.telephonyHold(channelId as string, !held);
      setHeld((h) => !h);
    }, held ? "Resumed" : "On hold");

  const doTransfer = () =>
    guard(async () => {
      if (!xferTarget) throw new Error("enter a transfer target");
      await api.telephonyTransfer(channelId as string, xferKind, xferTarget);
    }, "Transferred");

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
        <span
          className="badge"
          style={{ background: pill[state.status] || "#555", color: "#fff" }}
        >
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

        {state.status === "ringing" && (
          <button onClick={() => void answer()}>Answer</button>
        )}
        {(state.status === "in-call" || state.status === "ringing") && (
          <button onClick={() => void hangup()}>Hang up</button>
        )}
        <button disabled={!canControl || busy} onClick={toggleHold}>
          {held ? "Resume" : "Hold"}
        </button>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        <span className="muted">Blind transfer:</span>
        <select value={xferKind} onChange={(e) => setXferKind(e.target.value as TransferKind)}>
          <option value="did">DID</option>
          <option value="operator">Operator</option>
          <option value="ai_agent">AI Agent</option>
        </select>
        <input
          placeholder={xferKind === "did" ? "+1305…" : xferKind === "operator" ? "operator email" : "agent id"}
          value={xferTarget}
          onChange={(e) => setXferTarget(e.target.value)}
        />
        <button disabled={!canControl || busy} onClick={doTransfer}>
          Transfer
        </button>
      </div>

      {state.error && <div className="muted">Softphone: {state.error}</div>}
      {note && <div className="muted">{note}</div>}
      {!channelId && (
        <div className="muted">
          Select an in-progress platform call to enable hold/transfer.
        </div>
      )}
    </div>
  );
}

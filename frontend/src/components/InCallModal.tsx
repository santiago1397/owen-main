// Global in-call panel — the Quo-style "you are on a call" UI.
//
// Before this, the ONLY way to hang up was a small "Hang up" button inside <InCallBar>, which
// renders on one page and is hidden unless the softphone status is in-call/ringing. An operator
// on a call from anywhere else in the app had no way to end it. This mounts once in App.tsx
// beside <IncomingCallModal>, so the controls follow the call across every page.
//
// Deliberately NOT a full-screen overlay: it's a docked panel, so the operator can keep using
// the app (look up the caller, take notes) while talking. Hold/blind-transfer stay in
// <InCallBar> because they need the call's channel id, which the softphone doesn't know.
import { useEffect, useState } from "react";
import { Grid3x3, Mic, MicOff, PhoneOff } from "lucide-react";
import { api } from "../api";
import { useSoftphoneContext } from "../lib/softphoneContext";

function fmtPhone(p: string | null | undefined): string {
  const raw = String(p || "").trim();
  if (!raw) return "";
  const d = raw.replace(/[^\d]/g, "");
  if (d.length === 11 && d.startsWith("1")) {
    return `(${d.slice(1, 4)}) ${d.slice(4, 7)}-${d.slice(7)}`;
  }
  if (d.length === 10) return `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}`;
  return raw;
}

function fmtDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"];

export default function InCallModal() {
  const { state, hangup, toggleMute, sendDtmf } = useSoftphoneContext();
  const [now, setNow] = useState(() => Date.now());
  const [showKeypad, setShowKeypad] = useState(false);
  const [name, setName] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const inCall = state.status === "in-call";
  const peer = state.peer || "";

  // Tick the duration once a second, only while a call is up.
  useEffect(() => {
    if (!inCall) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [inCall]);

  // Reset per-call UI state so the keypad doesn't stay open into the next call.
  useEffect(() => {
    if (!inCall) {
      setShowKeypad(false);
      setName(null);
    }
  }, [inCall]);

  // Best-effort contact-name lookup; the number is always shown, so a failure changes nothing.
  useEffect(() => {
    if (!inCall || !peer) return;
    let cancelled = false;
    api
      .incomingContext(peer, "")
      .then((r: { caller_name: string | null }) => !cancelled && setName(r?.caller_name || null))
      .catch(() => !cancelled && setName(null));
    return () => {
      cancelled = true;
    };
  }, [inCall, peer]);

  if (!inCall) return null;

  const duration = state.answeredAt ? fmtDuration(now - state.answeredAt) : "0:00";
  const btn = {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    padding: "10px 14px",
    borderRadius: 10,
  } as const;

  return (
    <div
      role="dialog"
      aria-label="Call in progress"
      style={{
        position: "fixed",
        right: 16,
        bottom: 16,
        zIndex: 1000,
        width: "min(340px, calc(100vw - 32px))",
      }}
    >
      <div className="card" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
        <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 }}>
          <span className="muted" style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: 1 }}>
            On call
          </span>
          <span style={{ fontVariantNumeric: "tabular-nums" }}>{duration}</span>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <div style={{ fontSize: 18, fontWeight: 700 }}>
            {name || fmtPhone(peer) || "Connected"}
          </div>
          {name && <div className="muted">{fmtPhone(peer)}</div>}
        </div>

        {showKeypad && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 6 }}>
            {KEYS.map((k) => (
              <button key={k} onClick={() => void sendDtmf(k)} style={{ padding: "10px 0" }}>
                {k}
              </button>
            ))}
          </div>
        )}

        <div style={{ display: "flex", gap: 8 }}>
          <button
            onClick={toggleMute}
            aria-pressed={state.muted}
            title={state.muted ? "Unmute" : "Mute"}
            style={{ ...btn, flex: 1, background: state.muted ? "#8a6d3b" : undefined }}
          >
            {state.muted ? <MicOff size={16} /> : <Mic size={16} />}
            {state.muted ? "Muted" : "Mute"}
          </button>
          <button
            onClick={() => setShowKeypad((v) => !v)}
            aria-pressed={showKeypad}
            title="Keypad"
            style={{ ...btn }}
          >
            <Grid3x3 size={16} />
          </button>
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await hangup();
              } finally {
                setBusy(false);
              }
            }}
            title="Hang up"
            style={{ ...btn, flex: 1, background: "#8e1f1f", color: "#fff" }}
          >
            <PhoneOff size={16} /> End
          </button>
        </div>
      </div>
    </div>
  );
}

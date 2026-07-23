// Global incoming-call popup (Ticket 18).
//
// When a BulkVS/Asterisk DID has NO flow assigned, the backend rings every AVAILABLE operator's
// softphone at once (see backend runtime._handle_unassigned). Asterisk stamps each operator leg's
// caller-ID as `<dialed DID> <caller number>`, so the pending INVITE tells us BOTH who is calling
// and which number they dialed. This modal renders that, enriched to contact/DID names, with
// Answer / Decline — the Quo-style "pick up from anywhere in the app" experience.
//
// Mounted once in the app shell (App.tsx Layout) so it pops on ANY page, backed by the single
// shared softphone instance in SoftphoneProvider.
import { useEffect, useState } from "react";
import { PhoneCall, PhoneOff } from "lucide-react";
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

type Ctx = { caller_name: string | null; dialed_label: string | null };

export default function IncomingCallModal() {
  const { state, answer, decline } = useSoftphoneContext();
  const [ctx, setCtx] = useState<Ctx | null>(null);
  const [busy, setBusy] = useState(false);

  const ringing = state.status === "ringing" && !!state.incoming;
  const caller = state.incoming?.caller || "";
  const dialed = state.incoming?.dialed || "";

  // Enrich the two numbers to names once per ringing call (best-effort — the raw digits are
  // always shown as the fallback, so a failed lookup never blocks answering).
  useEffect(() => {
    if (!ringing) {
      setCtx(null);
      return;
    }
    let cancelled = false;
    api
      .incomingContext(caller, dialed)
      .then((r: Ctx) => !cancelled && setCtx(r))
      .catch(() => !cancelled && setCtx(null));
    return () => {
      cancelled = true;
    };
  }, [ringing, caller, dialed]);

  if (!ringing) return null;

  const whoName = ctx?.caller_name || null;
  const whoNumber = fmtPhone(caller) || "Unknown caller";
  const toLabel = ctx?.dialed_label || null;
  const toNumber = fmtPhone(dialed);

  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-label="Incoming call"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 1000,
        background: "rgba(0,0,0,0.55)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <div
        className="card"
        style={{
          width: "min(420px, 92vw)",
          padding: 24,
          display: "flex",
          flexDirection: "column",
          gap: 16,
          textAlign: "center",
        }}
      >
        <div className="muted" style={{ letterSpacing: 1, fontSize: 12, textTransform: "uppercase" }}>
          Incoming call
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <div style={{ fontSize: 22, fontWeight: 700 }}>{whoName || whoNumber}</div>
          {whoName && <div className="muted">{whoNumber}</div>}
        </div>

        {(toLabel || toNumber) && (
          <div className="muted" style={{ fontSize: 13 }}>
            to <strong>{toLabel || toNumber}</strong>
            {toLabel && toNumber ? ` · ${toNumber}` : ""}
          </div>
        )}

        <div style={{ display: "flex", gap: 12, justifyContent: "center", marginTop: 4 }}>
          <button
            disabled={busy}
            onClick={() => void act(decline)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              padding: "10px 18px",
              background: "#8e1f1f",
              color: "#fff",
            }}
          >
            <PhoneOff size={16} /> Decline
          </button>
          <button
            disabled={busy}
            onClick={() => void act(answer)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              padding: "10px 18px",
              background: "#2e7d32",
              color: "#fff",
            }}
          >
            <PhoneCall size={16} /> Answer
          </button>
        </div>
      </div>
    </div>
  );
}

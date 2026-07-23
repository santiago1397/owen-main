// In-call bar (Ticket 13) — fills ticket-06's reserved slot on the Calls Platform tab.
//
// Answer / hangup drive the operator's own SIP.js leg. Hold / blind-transfer are driven by
// the BACKEND over ARI (never browser->ARI) and act on the CALLER channel — which in this
// codebase is keyed by the call's Linkedid (== provider_call_sid), so we pass that as the
// channel id. An availability toggle registers/unregisters the softphone (unavailable =>
// the interpreter's operator-target dial finds the endpoint offline => default_fallback).
import { useEffect, useRef, useState } from "react";
import { Settings } from "lucide-react";
import { api } from "../api";
import {
  DIAL_EVENT,
  getLastFromNumber,
  setLastFromNumber,
  takePendingDial,
} from "../lib/dialer";
import {
  applySink,
  getAudioPref,
  setAudioPref,
  startRingtone,
  stopRingtone,
  useAudioDevices,
  type AudioKind,
} from "../lib/audioDevices";
import { useSoftphoneContext } from "../lib/softphoneContext";

type TransferKind = "did" | "operator" | "ai_agent";
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

// Gear → audio settings (Quo-style): pick microphone, speaker, and ringtone output device.
// Selections persist per-browser (localStorage) and apply live — speaker/ringtone via
// setSinkId on the existing elements, mic on the next call. A popover anchored to the gear.
function AudioSettings() {
  const [open, setOpen] = useState(false);
  const { devices, error, refresh } = useAudioDevices();
  const wrapRef = useRef<HTMLDivElement>(null);
  // Re-render on selection so the <select value> reflects the saved pref.
  const [, force] = useState(0);

  // Close on outside click / Escape.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const remoteEl = () =>
    document.getElementById("softphone-remote-audio") as HTMLAudioElement | null;

  const onPick = (kind: AudioKind, deviceId: string) => {
    setAudioPref(kind, deviceId);
    force((n) => n + 1);
    // Apply immediately where we can (mic takes effect on the next call).
    if (kind === "speaker") void applySink(remoteEl(), "speaker");
    if (kind === "ringtone") {
      // Brief preview so the operator hears which device the ring lands on.
      void startRingtone();
      window.setTimeout(stopRingtone, 1500);
    }
  };

  const Row = ({ kind, label, options }: { kind: AudioKind; label: string; options: { deviceId: string; label: string }[] }) => (
    <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <span className="muted" style={{ fontSize: 12 }}>{label}</span>
      <select value={getAudioPref(kind)} onChange={(e) => onPick(kind, e.target.value)}>
        <option value="">System default</option>
        {options.map((d) => (
          <option key={d.deviceId} value={d.deviceId}>{d.label}</option>
        ))}
      </select>
    </label>
  );

  return (
    <div ref={wrapRef} style={{ position: "relative", display: "inline-flex" }}>
      <button
        title="Audio settings"
        aria-label="Audio settings"
        onClick={() => {
          const next = !open;
          setOpen(next);
          if (next) void refresh();
        }}
        style={{ display: "inline-flex", alignItems: "center", padding: 6 }}
      >
        <Settings size={16} />
      </button>
      {open && (
        <div
          className="card"
          style={{
            position: "absolute",
            top: "calc(100% + 6px)",
            right: 0,
            zIndex: 20,
            width: 260,
            display: "flex",
            flexDirection: "column",
            gap: 10,
            padding: 12,
          }}
        >
          <div style={{ fontWeight: 600 }}>Audio settings</div>
          <Row kind="mic" label="Microphone" options={devices.inputs} />
          <Row kind="speaker" label="Speaker" options={devices.outputs} />
          <Row kind="ringtone" label="Ringtone" options={devices.outputs} />
          {error && <div className="muted" style={{ fontSize: 12 }}>{error}</div>}
          <div className="muted" style={{ fontSize: 11 }}>
            Microphone applies to your next call.
          </div>
        </div>
      )}
    </div>
  );
}

// `channelId` is the caller channel to hold/transfer (the selected/active platform call's
// provider_call_sid). Optional: without one, hold/transfer are disabled with a hint.
export default function InCallBar({ channelId }: { channelId?: string }) {
  const { state, setAvailable, answer, hangup } = useSoftphoneContext();
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
        <AudioSettings />
      </div>

      {/* Ticket 14: manual operator outbound calling (from-number picker + consent + bridge). */}
      <Dialer />

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

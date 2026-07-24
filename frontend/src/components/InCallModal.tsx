// Global in-call panel — the Quo-style "you are on a call" card.
//
// One rich, DRAGGABLE call surface that follows the operator across every page (mounted once in
// App.tsx). It renders only while status === "in-call"; the ringing state stays in
// <IncomingCallModal>. Beyond mute / keypad / hangup it now owns the app's real call
// capabilities: HOLD and BLIND-TRANSFER over ARI (backend-driven — SIP.js never touches ARI),
// an expanding dialpad side panel, a recording indicator, and an overflow menu with audio-device
// settings + a read-only availability status.
//
// The one thing SIP.js can't give us is the caller/callee CHANNEL id that hold/transfer act on.
// It comes from lib/activeCall: stamped exactly for outbound (from the originate response) and
// best-effort correlated here for inbound (peer number -> in-progress platform call). Until it
// resolves, hold/transfer render disabled with a hint (same contract as the old InCallBar).
import { useCallback, useEffect, useRef, useState } from "react";
import {
  BarChart3,
  Grid3x3,
  Mic,
  MicOff,
  MoreHorizontal,
  Pause,
  PhoneOff,
  Play,
  UserPlus,
  X,
} from "lucide-react";
import { api } from "../api";
import { setActiveCall, useActiveCall } from "../lib/activeCall";
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
import Avatar from "./Avatar";

type TransferKind = "did" | "operator" | "ai_agent";

const POS_KEY = "owen_incall_pos"; // remembered drag position (per browser)
const KEYS: { d: string; sub?: string }[] = [
  { d: "1" }, { d: "2", sub: "ABC" }, { d: "3", sub: "DEF" },
  { d: "4", sub: "GHI" }, { d: "5", sub: "JKL" }, { d: "6", sub: "MNO" },
  { d: "7", sub: "PQRS" }, { d: "8", sub: "TUV" }, { d: "9", sub: "WXYZ" },
  { d: "*" }, { d: "0", sub: "+" }, { d: "#" },
];

function onlyDigits(p: string | null | undefined): string {
  return String(p || "").replace(/\D/g, "");
}
function fmtPhone(p: string | null | undefined): string {
  const raw = String(p || "").trim();
  if (!raw) return "";
  const d = onlyDigits(raw);
  if (d.length === 11 && d.startsWith("1")) return `(${d.slice(1, 4)}) ${d.slice(4, 7)}-${d.slice(7)}`;
  if (d.length === 10) return `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}`;
  return raw;
}
function fmtDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}
function readPos(): { x: number; y: number } | null {
  try {
    const raw = localStorage.getItem(POS_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw);
    if (typeof p?.x === "number" && typeof p?.y === "number") return p;
  } catch {
    /* ignore malformed */
  }
  return null;
}

// A round icon button — the Quo control-bar / keypad idiom. `tone` colours the active states.
function CircleBtn({
  onClick, title, disabled, active, tone, size = 44, children,
}: {
  onClick?: () => void;
  title: string;
  disabled?: boolean;
  active?: boolean;
  tone?: "green" | "red" | "neutral";
  size?: number;
  children: any;
}) {
  const bg =
    tone === "green" ? "var(--good)" :
    tone === "red" ? "var(--danger)" :
    active ? "var(--panel)" : "var(--panel2)";
  const color = tone === "green" || tone === "red" ? "#0b0d10" : "var(--text)";
  return (
    <button
      onClick={onClick}
      title={title}
      aria-label={title}
      aria-pressed={active}
      disabled={disabled}
      style={{
        width: size, height: size, minWidth: size, borderRadius: "50%",
        border: active ? "1px solid var(--accent)" : "1px solid var(--border)",
        background: bg, color, display: "inline-flex", alignItems: "center",
        justifyContent: "center", padding: 0, opacity: disabled ? 0.45 : 1,
        cursor: disabled ? "not-allowed" : "pointer",
      }}
    >
      {children}
    </button>
  );
}

export default function InCallModal() {
  const { state, hangup, toggleMute, sendDtmf } = useSoftphoneContext();
  const active = useActiveCall();
  const inCall = state.status === "in-call";
  const peer = state.peer || "";

  const [now, setNow] = useState(() => Date.now());
  const [showKeypad, setShowKeypad] = useState(false);
  const [showXfer, setShowXfer] = useState(false);
  const [showMore, setShowMore] = useState(false);
  const [held, setHeld] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const [peerName, setPeerName] = useState<string | null>(null);
  const [lineLabel, setLineLabel] = useState<string | null>(null);
  const [meEmail, setMeEmail] = useState<string | null>(null);

  const [xferKind, setXferKind] = useState<TransferKind>("did");
  const [xferTarget, setXferTarget] = useState("");

  const rootRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(() => readPos());
  const drag = useRef<{ dx: number; dy: number } | null>(null);

  const channelId = active.channelId;
  const canControl = !!channelId; // hold / transfer need the caller channel

  // Tick the duration once a second while a call is up.
  useEffect(() => {
    if (!inCall) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [inCall]);

  // Reset per-call UI so nothing leaks into the next call.
  useEffect(() => {
    if (!inCall) {
      setShowKeypad(false);
      setShowXfer(false);
      setShowMore(false);
      setHeld(false);
      setNote(null);
      setPeerName(null);
      setLineLabel(null);
      setXferTarget("");
    }
  }, [inCall]);

  // Who am I (the "You" participant row). Fetched once; email is all the backend exposes.
  useEffect(() => {
    let cancelled = false;
    api.me().then((r: { email?: string }) => !cancelled && setMeEmail(r?.email || null)).catch(() => {});
    return () => { cancelled = true; };
  }, []);

  // Peer name (+ inbound line label) — best-effort; raw digits are the always-present fallback.
  useEffect(() => {
    if (!inCall || !peer) return;
    let cancelled = false;
    const dialed = active.direction === "inbound" ? active.line || "" : "";
    api
      .incomingContext(peer, dialed)
      .then((r: { caller_name: string | null; dialed_label: string | null }) => {
        if (cancelled) return;
        setPeerName(r?.caller_name || null);
        if (active.direction === "inbound") setLineLabel(r?.dialed_label || null);
      })
      .catch(() => !cancelled && setPeerName(null));
    return () => { cancelled = true; };
  }, [inCall, peer, active.direction, active.line]);

  // Outbound line label = the from-number's friendly name (else the formatted number).
  useEffect(() => {
    if (!inCall || active.direction !== "outbound" || !active.line) return;
    let cancelled = false;
    api
      .outboundFromNumbers()
      .then((rows: { phone_number: string; friendly_name?: string | null }[]) => {
        if (cancelled) return;
        const m = rows.find((r) => onlyDigits(r.phone_number) === onlyDigits(active.line));
        setLineLabel(m?.friendly_name || null);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [inCall, active.direction, active.line]);

  // Inbound channel-id correlation: no id rides the INVITE, so match the peer number against the
  // in-progress platform calls until the row appears. Best-effort, polls a few seconds.
  useEffect(() => {
    if (!inCall || active.direction !== "inbound" || channelId || !peer) return;
    let cancelled = false;
    const target = onlyDigits(peer);
    const tryMatch = async () => {
      try {
        const res: any = await api.calls({ provider_group: "platform", page: 1, page_size: 20 });
        const hit = (res?.items || []).find((c: any) => onlyDigits(c.caller_number) === target);
        if (!cancelled && hit?.provider_call_sid) setActiveCall({ channelId: hit.provider_call_sid });
      } catch {
        /* keep trying */
      }
    };
    void tryMatch();
    const t = setInterval(tryMatch, 3000);
    return () => { cancelled = true; clearInterval(t); };
  }, [inCall, active.direction, channelId, peer]);

  // Dragging by the header. Pointer math is relative to the current rect so the grab point stays
  // under the cursor; the result is clamped to the viewport.
  const onDragMove = useCallback((e: MouseEvent) => {
    if (!drag.current) return;
    const el = rootRef.current;
    const w = el?.offsetWidth ?? 320;
    const h = el?.offsetHeight ?? 200;
    const x = Math.min(Math.max(0, e.clientX - drag.current.dx), window.innerWidth - w);
    const y = Math.min(Math.max(0, e.clientY - drag.current.dy), window.innerHeight - h);
    setPos({ x, y });
  }, []);
  const onDragEnd = useCallback(() => {
    drag.current = null;
    window.removeEventListener("mousemove", onDragMove);
    window.removeEventListener("mouseup", onDragEnd);
    setPos((p) => {
      if (p) localStorage.setItem(POS_KEY, JSON.stringify(p));
      return p;
    });
  }, [onDragMove]);
  const onDragStart = (e: React.MouseEvent) => {
    // Interacting with a header control (the transfer button) must not begin a drag.
    if ((e.target as HTMLElement).closest("button, select, input")) return;
    const el = rootRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    drag.current = { dx: e.clientX - r.left, dy: e.clientY - r.top };
    // First-ever drag: seed pos from the current (docked) rect so it doesn't jump.
    if (!pos) setPos({ x: r.left, y: r.top });
    window.addEventListener("mousemove", onDragMove);
    window.addEventListener("mouseup", onDragEnd);
  };
  useEffect(() => () => {
    window.removeEventListener("mousemove", onDragMove);
    window.removeEventListener("mouseup", onDragEnd);
  }, [onDragMove, onDragEnd]);

  const guard = async (fn: () => Promise<any>, ok?: string) => {
    setBusy(true);
    setNote(null);
    try {
      await fn();
      if (ok) setNote(ok);
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
      if (!xferTarget.trim()) throw new Error("enter a transfer target");
      await api.telephonyTransfer(channelId as string, xferKind, xferTarget.trim());
      setShowXfer(false);
    }, "Transferred");

  if (!inCall) return null;

  const duration = state.answeredAt ? fmtDuration(now - state.answeredAt) : "0:00";
  const header = lineLabel || fmtPhone(active.line) || "On call";
  const meName = meEmail ? meEmail.split("@")[0] : "You";
  // Keypad opens on whichever side has room, so the panel never runs off-screen.
  const centerX = pos ? pos.x + (rootRef.current?.offsetWidth ?? 320) / 2 : window.innerWidth;
  const keypadLeft = centerX > window.innerWidth / 2;

  const positioned: React.CSSProperties = pos
    ? { left: pos.x, top: pos.y }
    : { right: 16, bottom: 16 };

  return (
    <div
      ref={rootRef}
      role="dialog"
      aria-label="Call in progress"
      style={{
        position: "fixed", zIndex: 1000, display: "flex",
        flexDirection: keypadLeft ? "row-reverse" : "row", alignItems: "flex-start", gap: 10,
        ...positioned,
      }}
    >
      {/* --- the call card --- */}
      <div
        className="card"
        style={{ width: "min(340px, calc(100vw - 32px))", padding: 0, overflow: "hidden" }}
      >
        {/* header (drag handle) */}
        <div
          onMouseDown={onDragStart}
          style={{
            display: "flex", alignItems: "center", gap: 8, padding: "12px 14px",
            cursor: "grab", userSelect: "none", borderBottom: "1px solid var(--border)",
          }}
        >
          <span style={{ fontSize: 16 }}>🤝</span>
          <span style={{ fontWeight: 700, fontSize: 13.5, minWidth: 0, overflow: "hidden",
                         textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {header}
          </span>
          <span className="muted" style={{ fontSize: 12, fontVariantNumeric: "tabular-nums" }}>
            {duration}
          </span>
          <div style={{ flex: 1 }} />
          <BarChart3 size={16} color="var(--muted)" />
          <button
            title="Transfer call" aria-label="Transfer call"
            onClick={() => { setShowXfer((v) => !v); setShowMore(false); }}
            style={{ background: "none", border: "none", padding: 2, color: "var(--text)" }}
          >
            <UserPlus size={16} />
          </button>
        </div>

        {/* participants */}
        <div style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <Avatar name={meName} number={null} size={30} />
            <div style={{ fontWeight: 600, fontSize: 13.5 }}>{meName}</div>
            <span className="muted" style={{ fontSize: 12 }}>You</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <Avatar name={peerName} number={peer} size={30} />
            <div style={{ minWidth: 0 }}>
              <div style={{ fontWeight: 600, fontSize: 13.5, overflow: "hidden",
                            textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {peerName || fmtPhone(peer) || "Connected"}
              </div>
              {peerName && <div className="muted" style={{ fontSize: 11.5 }}>{fmtPhone(peer)}</div>}
            </div>
          </div>
        </div>

        {/* transfer popover */}
        {showXfer && (
          <div style={{ padding: "0 14px 12px", display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ display: "flex", gap: 6 }}>
              <select value={xferKind} onChange={(e) => setXferKind(e.target.value as TransferKind)}>
                <option value="did">DID</option>
                <option value="operator">Operator</option>
                <option value="ai_agent">AI Agent</option>
              </select>
              <input
                style={{ flex: 1, minWidth: 0 }}
                placeholder={xferKind === "did" ? "+1305…" : xferKind === "operator" ? "operator email" : "agent id"}
                value={xferTarget}
                onChange={(e) => setXferTarget(e.target.value)}
              />
            </div>
            <button className="primary" disabled={!canControl || busy} onClick={doTransfer}>
              Transfer
            </button>
            {!canControl && (
              <div className="muted" style={{ fontSize: 11 }}>
                Waiting for the call channel — transfer enables once it's connected.
              </div>
            )}
          </div>
        )}

        {/* overflow menu: audio settings + availability */}
        {showMore && <MoreMenu availability={state.available ? "available" : state.status} />}

        {/* control bar */}
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          gap: 6, padding: "12px 14px", borderTop: "1px solid var(--border)",
        }}>
          <CircleBtn title={state.muted ? "Unmute" : "Mute"} tone="green" onClick={toggleMute} active={state.muted}>
            {state.muted ? <MicOff size={18} /> : <Mic size={18} />}
          </CircleBtn>
          <div title="Recording" aria-label="Recording"
               style={{ display: "inline-flex", flexDirection: "column", alignItems: "center",
                        justifyContent: "center", width: 44, gap: 2 }}>
            <span style={{ width: 10, height: 10, borderRadius: "50%", background: "var(--danger)" }} />
            <span style={{ fontSize: 10, color: "var(--danger)", fontWeight: 600 }}>Rec</span>
          </div>
          <CircleBtn title="Keypad" onClick={() => setShowKeypad((v) => !v)} active={showKeypad}>
            <Grid3x3 size={18} />
          </CircleBtn>
          <CircleBtn title={held ? "Resume" : "Hold"} onClick={toggleHold} active={held}
                     disabled={!canControl || busy}>
            {held ? <Play size={18} /> : <Pause size={18} />}
          </CircleBtn>
          <CircleBtn title="More" onClick={() => { setShowMore((v) => !v); setShowXfer(false); }} active={showMore}>
            <MoreHorizontal size={18} />
          </CircleBtn>
          <CircleBtn title="Hang up" tone="red" disabled={busy}
                     onClick={() => void guard(hangup)}>
            <PhoneOff size={18} />
          </CircleBtn>
        </div>

        {note && <div className="muted" style={{ fontSize: 11.5, padding: "0 14px 10px" }}>{note}</div>}
      </div>

      {/* --- dialpad side panel (Quo #9) --- */}
      {showKeypad && (
        <div className="card" style={{ width: 240, padding: 14 }}>
          <div style={{ display: "flex", alignItems: "center", marginBottom: 10 }}>
            <div style={{ fontWeight: 600, fontSize: 13, flex: 1 }}>Keypad</div>
            <button title="Close keypad" aria-label="Close keypad" onClick={() => setShowKeypad(false)}
                    style={{ background: "none", border: "none", color: "var(--muted)", padding: 2 }}>
              <X size={16} />
            </button>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8, justifyItems: "center" }}>
            {KEYS.map((k) => (
              <button
                key={k.d}
                onClick={() => void sendDtmf(k.d)}
                style={{
                  width: 60, height: 60, borderRadius: "50%", background: "var(--panel2)",
                  border: "1px solid var(--border)", display: "flex", flexDirection: "column",
                  alignItems: "center", justifyContent: "center", gap: 1, cursor: "pointer",
                }}
              >
                <span style={{ fontSize: 20, fontWeight: 600, lineHeight: 1 }}>{k.d}</span>
                {k.sub && <span style={{ fontSize: 8.5, letterSpacing: 1, color: "var(--muted)" }}>{k.sub}</span>}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// The "..." overflow: audio-device pickers (moved here from the old InCallBar) + a read-only
// availability status. Availability is TOGGLED on the page (InCallBar); here it is status only.
function MoreMenu({ availability }: { availability: string }) {
  const { devices, error, refresh } = useAudioDevices();
  const [, force] = useState(0);
  useEffect(() => { void refresh(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const remoteEl = () => document.getElementById("softphone-remote-audio") as HTMLAudioElement | null;
  const onPick = (kind: AudioKind, deviceId: string) => {
    setAudioPref(kind, deviceId);
    force((n) => n + 1);
    if (kind === "speaker") void applySink(remoteEl(), "speaker");
    if (kind === "ringtone") { void startRingtone(); window.setTimeout(stopRingtone, 1200); }
  };
  const Row = ({ kind, label, options }: { kind: AudioKind; label: string; options: { deviceId: string; label: string }[] }) => (
    <label style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <span className="muted" style={{ fontSize: 11 }}>{label}</span>
      <select value={getAudioPref(kind)} onChange={(e) => onPick(kind, e.target.value)}>
        <option value="">System default</option>
        {options.map((d) => <option key={d.deviceId} value={d.deviceId}>{d.label}</option>)}
      </select>
    </label>
  );
  return (
    <div style={{ padding: "0 14px 12px", display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span className="muted" style={{ fontSize: 11 }}>Status</span>
        <span className="badge" style={{ textTransform: "capitalize" }}>{availability}</span>
      </div>
      <Row kind="mic" label="Microphone" options={devices.inputs} />
      <Row kind="speaker" label="Speaker" options={devices.outputs} />
      <Row kind="ringtone" label="Ringtone" options={devices.outputs} />
      {error && <div className="muted" style={{ fontSize: 11 }}>{error}</div>}
    </div>
  );
}

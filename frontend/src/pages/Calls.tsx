import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { API_BASE, api } from "../api";
import DateRangeBar from "../components/DateRangeBar";
import { type Range } from "../lib/dates";

function RecordingPlayer({ recordingId }: { recordingId: string }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    api.playUrl(recordingId).then((r) => setUrl(API_BASE + r.url)).catch(() => setUrl(null));
  }, [recordingId]);
  if (!url) return <div className="muted">Loading audio…</div>;
  return <audio controls src={url} style={{ width: "100%" }} />;
}

type Segment = { speaker: string; start?: number | null; end?: number | null; text: string };

// Two-sided "who said what" view for dual-channel (stereo) recordings. Caller on the
// left, operator on the right; falls back to the flat transcript when segments are absent.
function TranscriptThread({ segments }: { segments: Segment[] }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {segments.map((s, i) => {
        const isCaller = s.speaker === "caller";
        return (
          <div
            key={i}
            style={{
              alignSelf: isCaller ? "flex-start" : "flex-end",
              maxWidth: "80%",
              background: isCaller ? "var(--bubble-caller, #1e2a3a)" : "var(--bubble-operator, #23331f)",
              borderRadius: 10,
              padding: "6px 10px",
            }}
          >
            <div className="muted" style={{ fontSize: 11, marginBottom: 2 }}>
              {isCaller ? "Caller" : "Operator"}
            </div>
            <div style={{ whiteSpace: "pre-wrap" }}>{s.text}</div>
          </div>
        );
      })}
    </div>
  );
}

function CallDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const qc = useQueryClient();
  const { data: c } = useQuery({ queryKey: ["call", id], queryFn: () => api.call(id) });
  const { data: settings } = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  if (!c) return null;

  const override = async (body: any) => {
    await api.overrideAnalysis(id, body);
    qc.invalidateQueries({ queryKey: ["call", id] });
    qc.invalidateQueries({ queryKey: ["calls"] });
  };

  return (
    <>
      <div className="overlay" onClick={onClose} />
      <div className="drawer">
        <div style={{ display: "flex", justifyContent: "space-between" }}>
          <h3 style={{ margin: 0 }}>Call detail</h3>
          <button onClick={onClose}>✕</button>
        </div>
        <div className="kv">
          <span className="muted">Provider</span><span>{c.provider}</span>
          <span className="muted">Caller</span><span>{c.caller_number || "—"}</span>
          <span className="muted">Dialed</span><span>{c.dialed_number || "—"}{c.dialed_number_label ? ` (${c.dialed_number_label})` : ""}</span>
          <span className="muted">Campaign</span><span>{c.campaign_name || "—"}</span>
          <span className="muted">Status</span><span>{c.status || "—"}</span>
          <span className="muted">Started</span><span>{c.started_at ? new Date(c.started_at).toLocaleString() : "—"}</span>
          <span className="muted">Duration</span><span>{c.duration_seconds ?? "—"}s</span>
        </div>

        {c.recordings?.length > 0 && (
          <div className="card" style={{ marginBottom: 12 }}>
            <div className="l" style={{ marginBottom: 8 }}>Recording</div>
            {c.recordings.map((r: any) =>
              r.available ? <RecordingPlayer key={r.id} recordingId={r.id} />
                          : <div key={r.id} className="muted">Not downloaded yet</div>)}
          </div>
        )}

        {c.analysis && (
          <div className="card" style={{ marginBottom: 12 }}>
            <div className="l" style={{ marginBottom: 8 }}>Analysis</div>
            <div style={{ marginBottom: 8 }}>
              {c.is_spam ? <span className="badge spam">SPAM</span> : <span className="badge">not spam</span>}
              {" "}<span className="badge">{c.category || "—"}</span>
              {" "}<span className="muted">conf {c.analysis.spam_confidence ?? "—"}</span>
            </div>
            {c.analysis.summary && <p className="muted">{c.analysis.summary}</p>}
            {c.analysis.tags?.length > 0 &&
              <div>{c.analysis.tags.map((t: string) =>
                <span key={t} className={`badge${t === "job" ? " job" : ""}`} style={{ marginRight: 4 }}>{t}</span>)}</div>}
            <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
              <select defaultValue={c.category || ""} onChange={(e) => override({ category_override: e.target.value })}>
                <option value="">override category…</option>
                {(settings?.categories || []).map((cat: string) => <option key={cat} value={cat}>{cat}</option>)}
              </select>
              <button onClick={() => override({ is_spam_override: !c.is_spam })}>
                Mark {c.is_spam ? "not spam" : "spam"}
              </button>
            </div>
          </div>
        )}

        {(c.transcript_segments?.length > 0 || c.transcript) && (
          <div className="card" style={{ marginBottom: 12 }}>
            <div className="l" style={{ marginBottom: 8 }}>Transcript</div>
            {c.transcript_segments?.length > 0 ? (
              <TranscriptThread segments={c.transcript_segments} />
            ) : (
              <div className="muted" style={{ whiteSpace: "pre-wrap" }}>{c.transcript}</div>
            )}
          </div>
        )}

        <div className="card">
          <div className="l" style={{ marginBottom: 8 }}>Timeline</div>
          <ul className="timeline" style={{ margin: 0, paddingLeft: 18 }}>
            {c.events.map((e: any, i: number) => (
              <li key={i}>{e.event_type} <span className="muted">— {new Date(e.received_at).toLocaleTimeString()}</span></li>
            ))}
          </ul>
        </div>
      </div>
    </>
  );
}

export default function Calls() {
  const [filters, setFilters] = useState<any>({ page: 1, page_size: 50 });
  const [hideJunk, setHideJunk] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  // Provider split (Ticket 06): "attribution" = Twilio/SignalWire, "platform" = BulkVS/Asterisk.
  // Backed by the additive ?provider_group= param on /api/calls; the call drawer is reused as-is.
  const [tab, setTab] = useState<"attribution" | "platform">("attribution");
  const { data: campaigns } = useQuery({ queryKey: ["campaigns"], queryFn: api.campaigns });

  // The single "Hide failed & ≤3s" checkbox governs all junk-hiding. When unchecked we must
  // also opt into 0–1s calls via include_short, otherwise the backend's separate short-call
  // filter keeps hiding them and the checkbox appears to do nothing.
  const query = {
    ...filters,
    provider_group: tab,
    hide_junk: hideJunk || undefined,
    include_short: hideJunk ? undefined : true,
  };
  const { data } = useQuery({ queryKey: ["calls", query], queryFn: () => api.calls(query) });
  const set = (k: string, v: any) => setFilters((f: any) => ({ ...f, [k]: v, page: 1 }));
  const onRange = (r: Range | null) =>
    setFilters((f: any) => ({
      ...f, page: 1,
      date_from: r?.from.toISOString(),
      date_to: r?.to.toISOString(),
    }));

  return (
    <div>
      <div className="toolbar" style={{ flexWrap: "wrap", gap: 8 }}>
        <h2 style={{ marginTop: 0, marginBottom: 0, flex: 1 }}>Calls</h2>
        <DateRangeBar defaultPreset="7d" onChange={onRange} />
      </div>
      <div className="tabs">
        <button
          className={"tab" + (tab === "attribution" ? " active" : "")}
          onClick={() => { setTab("attribution"); set("provider", undefined); }}
        >
          Attribution
        </button>
        <button
          className={"tab" + (tab === "platform" ? " active" : "")}
          onClick={() => { setTab("platform"); set("provider", undefined); }}
        >
          Platform
        </button>
      </div>
      <div className="toolbar" style={{ flexWrap: "wrap", gap: 8, marginTop: 8 }}>
        <input placeholder="caller number…" onChange={(e) => set("caller", e.target.value)} />
        <select onChange={(e) => set("campaign_id", e.target.value || undefined)}>
          <option value="">all campaigns</option>
          {(campaigns || []).map((c: any) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select>
        <select value={filters.provider || ""} onChange={(e) => set("provider", e.target.value || undefined)}>
          <option value="">all providers</option>
          {(tab === "attribution"
            ? ["twilio", "signalwire"]
            : ["bulkvs", "asterisk"]
          ).map((p) => <option key={p} value={p}>{p}</option>)}
        </select>
        <select onChange={(e) => set("status", e.target.value)}>
          <option value="">any status</option>
          {["completed", "no-answer", "busy", "failed"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <label className="muted" style={{ display: "flex", alignItems: "center", gap: 6, marginLeft: "auto" }}>
          <input
            type="checkbox"
            checked={hideJunk}
            onChange={(e) => { setHideJunk(e.target.checked); setFilters((f: any) => ({ ...f, page: 1 })); }}
          />
          Hide failed &amp; ≤3s calls
        </label>
      </div>

      <div className="card">
        <table>
          <thead>
            <tr><th>When</th><th>Caller</th><th>Number</th><th>Campaign</th><th>Status</th><th>Dur</th><th>Flags</th></tr>
          </thead>
          <tbody>
            {(data?.items || []).map((c: any) => (
              <tr key={c.id} className="clickable" onClick={() => setSelected(c.id)}>
                <td>{c.started_at ? new Date(c.started_at).toLocaleString() : "—"}</td>
                <td>{c.caller_number || "—"}</td>
                <td>
                  {c.dialed_number || "—"}
                  {c.dialed_number_label && <div className="muted">{c.dialed_number_label}</div>}
                </td>
                <td>{c.campaign_name || "—"}</td>
                <td>{c.status || "—"}</td>
                <td>{c.duration_seconds ?? "—"}</td>
                <td>
                  {c.is_new_for_campaign && <span className="badge new">new</span>}{" "}
                  {c.is_spam && <span className="badge spam">spam</span>}{" "}
                  {c.has_recording && <span className="badge">🎧</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="toolbar" style={{ marginTop: 12 }}>
          <span className="muted">{data?.total ?? 0} calls</span>
          <div style={{ flex: 1 }} />
          <button disabled={filters.page <= 1}
                  onClick={() => setFilters((f: any) => ({ ...f, page: f.page - 1 }))}>Prev</button>
          <span>page {filters.page}</span>
          <button disabled={(data?.items?.length || 0) < filters.page_size}
                  onClick={() => setFilters((f: any) => ({ ...f, page: f.page + 1 }))}>Next</button>
        </div>
      </div>

      {selected && <CallDrawer id={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

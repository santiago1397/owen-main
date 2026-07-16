import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { API_BASE, api } from "../api";

function RecordingPlayer({ recordingId }: { recordingId: string }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    api.playUrl(recordingId).then((r) => setUrl(API_BASE + r.url)).catch(() => setUrl(null));
  }, [recordingId]);
  if (!url) return <div className="muted">Loading audio…</div>;
  return <audio controls src={url} style={{ width: "100%" }} />;
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
          <span className="muted">Dialed</span><span>{c.dialed_number || "—"}</span>
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
              <div>{c.analysis.tags.map((t: string) => <span key={t} className="badge" style={{ marginRight: 4 }}>{t}</span>)}</div>}
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

        {c.transcript && (
          <div className="card" style={{ marginBottom: 12 }}>
            <div className="l" style={{ marginBottom: 8 }}>Transcript</div>
            <div className="muted" style={{ whiteSpace: "pre-wrap" }}>{c.transcript}</div>
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
  const [selected, setSelected] = useState<string | null>(null);
  const { data } = useQuery({ queryKey: ["calls", filters], queryFn: () => api.calls(filters) });
  const set = (k: string, v: any) => setFilters((f: any) => ({ ...f, [k]: v, page: 1 }));

  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Calls</h2>
      <div className="toolbar">
        <input placeholder="caller number…" onChange={(e) => set("caller", e.target.value)} />
        <select onChange={(e) => set("provider", e.target.value)}>
          <option value="">all providers</option>
          <option value="twilio">twilio</option>
          <option value="signalwire">signalwire</option>
        </select>
        <select onChange={(e) => set("status", e.target.value)}>
          <option value="">any status</option>
          {["completed", "no-answer", "busy", "failed"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
        <label className="muted" style={{ display: "flex", alignItems: "center", gap: 6, marginLeft: "auto" }}>
          <input
            type="checkbox"
            checked={!!filters.include_short}
            onChange={(e) => set("include_short", e.target.checked || undefined)}
          />
          Show 0–1s calls
        </label>
      </div>

      <div className="card">
        <table>
          <thead>
            <tr><th>When</th><th>Caller</th><th>Campaign</th><th>Status</th><th>Dur</th><th>Flags</th></tr>
          </thead>
          <tbody>
            {(data?.items || []).map((c: any) => (
              <tr key={c.id} className="clickable" onClick={() => setSelected(c.id)}>
                <td>{c.started_at ? new Date(c.started_at).toLocaleString() : "—"}</td>
                <td>{c.caller_number || "—"}</td>
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

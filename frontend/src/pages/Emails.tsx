import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { api } from "../api";

// Human-friendly label + badge class for each relay outcome.
function RelayBadge({ status, relayed }: { status: string | null; relayed: boolean }) {
  if (relayed || status === "sent") return <span className="badge new">sent to GHL</span>;
  if (status === "skipped_not_configured")
    return <span className="badge" title="GHL email webhook not configured yet">GHL not configured</span>;
  if (status === "failed") return <span className="badge spam">relay failed</span>;
  if (status === "pending") return <span className="badge">relay queued…</span>;
  if (status === "skipped_not_parsed") return <span className="badge">not relayed</span>;
  return <span className="muted">—</span>;
}

function ParseBadge({ status }: { status: string }) {
  return status === "parsed"
    ? <span className="badge new">parsed</span>
    : <span className="badge spam">parse failed</span>;
}

function EmailDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const qc = useQueryClient();
  const { data: e } = useQuery({ queryKey: ["email", id], queryFn: () => api.email(id) });
  const [showRaw, setShowRaw] = useState(false);
  const [relaying, setRelaying] = useState(false);
  if (!e) return null;

  const relayNow = async () => {
    setRelaying(true);
    try {
      await api.relayEmail(id);
      qc.invalidateQueries({ queryKey: ["email", id] });
      qc.invalidateQueries({ queryKey: ["emails"] });
    } finally {
      setRelaying(false);
    }
  };

  return (
    <>
      <div className="overlay" onClick={onClose} />
      <div className="drawer">
        <div style={{ display: "flex", justifyContent: "space-between" }}>
          <h3 style={{ margin: 0 }}>Email detail</h3>
          <button onClick={onClose}>✕</button>
        </div>

        <div className="kv">
          <span className="muted">Received</span><span>{e.received_at ? new Date(e.received_at).toLocaleString() : "—"}</span>
          <span className="muted">From</span><span>{e.from_addr || "—"}</span>
          <span className="muted">Subject</span><span>{e.subject || "—"}</span>
          <span className="muted">Job ID</span><span>{e.job_id || "—"}</span>
          <span className="muted">Parse</span><span><ParseBadge status={e.parse_status} /></span>
          <span className="muted">Relay</span>
          <span>
            <RelayBadge status={e.relay_status} relayed={e.relayed_to_ghl} />
            {e.relayed_at && <span className="muted"> — {new Date(e.relayed_at).toLocaleString()}</span>}
          </span>
        </div>

        {e.parse_status !== "parsed" && (
          <div className="card" style={{ marginBottom: 12 }}>
            <div className="l" style={{ marginBottom: 8 }}>Why it wasn't relayed</div>
            <p className="muted" style={{ margin: 0 }}>
              {e.parse_error || "Parsing failed — the raw email is stored below for inspection."}
            </p>
          </div>
        )}

        {e.relay_error && (
          <div className="card" style={{ marginBottom: 12 }}>
            <div className="l" style={{ marginBottom: 8 }}>Last relay error</div>
            <pre className="muted" style={{ whiteSpace: "pre-wrap", margin: 0 }}>{e.relay_error}</pre>
          </div>
        )}

        {e.relay_result && (
          <div className="card" style={{ marginBottom: 12 }}>
            <div className="l" style={{ marginBottom: 8 }}>Created in GHL</div>
            <div className="kv">
              <span className="muted">Mode</span><span>{e.relay_result.mode}</span>
              {e.relay_result.contact_id && (<><span className="muted">Contact ID</span><span>{e.relay_result.contact_id}</span></>)}
              {e.relay_result.opportunity_id && (<><span className="muted">Opportunity ID</span><span>{e.relay_result.opportunity_id}</span></>)}
              {"note_added" in e.relay_result && (<><span className="muted">Note added</span><span>{e.relay_result.note_added ? "yes" : "no"}</span></>)}
            </div>
          </div>
        )}

        {e.fields && (
          <div className="card" style={{ marginBottom: 12 }}>
            <div className="l" style={{ marginBottom: 8 }}>Extracted fields</div>
            <div className="kv">
              {Object.entries(e.fields).map(([k, v]) => (
                <Fragment key={k}>
                  <span className="muted">{k}</span>
                  <span style={{ whiteSpace: "pre-wrap" }}>
                    {typeof v === "object" ? JSON.stringify(v, null, 2) : String(v)}
                  </span>
                </Fragment>
              ))}
            </div>
          </div>
        )}

        {e.parse_status === "parsed" && (
          <div className="card" style={{ marginBottom: 12 }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
              <div className="l">Payload {e.relayed_to_ghl ? "sent to" : "that would be sent to"} GHL</div>
              {!e.relayed_to_ghl && (
                <button onClick={relayNow} disabled={relaying}>
                  {relaying ? "Relaying…" : e.ghl_email_relay_configured ? "Relay now" : "Retry relay"}
                </button>
              )}
            </div>
            {!e.ghl_email_relay_configured && (
              <p className="muted" style={{ marginTop: 0 }}>
                GHL email webhook isn't configured yet, so nothing was sent. This is the exact JSON
                that will be POSTed once <code>GHL_EMAIL_WEBHOOK_URL</code> is set.
              </p>
            )}
            <pre style={{ whiteSpace: "pre-wrap", background: "var(--code-bg, #12161c)", padding: 10, borderRadius: 8, overflowX: "auto" }}>
              {JSON.stringify(e.ghl_payload, null, 2)}
            </pre>
          </div>
        )}

        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div className="l">Raw email</div>
            <button onClick={() => setShowRaw((s) => !s)}>{showRaw ? "Hide" : "Show"}</button>
          </div>
          {showRaw && (
            <pre style={{ whiteSpace: "pre-wrap", fontSize: 11, marginTop: 8, maxHeight: 400, overflow: "auto" }}>
              {e.raw}
            </pre>
          )}
        </div>
      </div>
    </>
  );
}

export default function Emails() {
  const [filters, setFilters] = useState<any>({ limit: 50, offset: 0 });
  const [selected, setSelected] = useState<string | null>(null);
  // Poll a little faster than the global 30s so a freshly-arrived email surfaces quickly.
  const { data } = useQuery({
    queryKey: ["emails", filters],
    queryFn: () => api.emails(filters),
    refetchInterval: 15000,
  });
  const set = (k: string, v: any) => setFilters((f: any) => ({ ...f, [k]: v || undefined, offset: 0 }));

  return (
    <div>
      <div className="toolbar" style={{ flexWrap: "wrap", gap: 8 }}>
        <h2 style={{ marginTop: 0, marginBottom: 0, flex: 1 }}>Email Log</h2>
        {data && !data.ghl_email_relay_configured && (
          <span className="badge" title="Emails are parsed and stored, but not yet forwarded to GHL">
            GHL relay not configured
          </span>
        )}
      </div>
      <p className="muted" style={{ marginTop: 4 }}>
        Every job-notification email pulled from the mailbox, and whether it was forwarded to GoHighLevel.
      </p>

      <div className="toolbar" style={{ flexWrap: "wrap", gap: 8, marginTop: 8 }}>
        <select onChange={(e) => set("parse_status", e.target.value)}>
          <option value="">any parse status</option>
          <option value="parsed">parsed</option>
          <option value="failed">parse failed</option>
        </select>
        <select onChange={(e) => set("relay_status", e.target.value)}>
          <option value="">any relay status</option>
          <option value="sent">sent to GHL</option>
          <option value="skipped_not_configured">GHL not configured</option>
          <option value="failed">relay failed</option>
        </select>
      </div>

      <div className="card">
        <table>
          <thead>
            <tr><th>Received</th><th>From</th><th>Subject</th><th>Job ID</th><th>Parse</th><th>Relay</th></tr>
          </thead>
          <tbody>
            {(data?.items || []).map((e: any) => (
              <tr key={e.id} className="clickable" onClick={() => setSelected(e.id)}>
                <td>{e.received_at ? new Date(e.received_at).toLocaleString() : "—"}</td>
                <td>{e.from_addr || "—"}</td>
                <td style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {e.subject || "—"}
                </td>
                <td>{e.job_id || "—"}</td>
                <td><ParseBadge status={e.parse_status} /></td>
                <td><RelayBadge status={e.relay_status} relayed={e.relayed_to_ghl} /></td>
              </tr>
            ))}
            {data && data.items.length === 0 && (
              <tr><td colSpan={6} className="muted" style={{ textAlign: "center", padding: 20 }}>
                No emails ingested yet.
              </td></tr>
            )}
          </tbody>
        </table>
        <div className="toolbar" style={{ marginTop: 12 }}>
          <span className="muted">{data?.total ?? 0} emails</span>
          <div style={{ flex: 1 }} />
          <button disabled={(filters.offset || 0) <= 0}
                  onClick={() => setFilters((f: any) => ({ ...f, offset: Math.max(0, (f.offset || 0) - f.limit) }))}>Prev</button>
          <button disabled={(data?.items?.length || 0) < filters.limit}
                  onClick={() => setFilters((f: any) => ({ ...f, offset: (f.offset || 0) + f.limit }))}>Next</button>
        </div>
      </div>

      {selected && <EmailDrawer id={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

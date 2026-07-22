import { useQuery } from "@tanstack/react-query";
import { api } from "../api";

type Flow = {
  id: string;
  name: string;
  active_version_id?: string | null;
  created_at?: string | null;
};

// Call Flows library (Ticket 06 shell): lists flows from the Ticket-02 flows API. One flow
// maps to many numbers. The actual rule-form authoring is a LATER ticket — the "new"/"open"
// affordances are present but disabled placeholders here.
export default function Flows() {
  const { data } = useQuery<Flow[]>({ queryKey: ["flows"], queryFn: api.flows });

  return (
    <div>
      <div className="toolbar" style={{ flexWrap: "wrap", gap: 8 }}>
        <h2 style={{ marginTop: 0, marginBottom: 0, flex: 1 }}>Call Flows</h2>
        <button disabled title="Flow authoring arrives in a later ticket">New flow (coming soon)</button>
      </div>
      <p className="muted" style={{ marginTop: 4 }}>
        Reusable call-handling graphs. One flow can be assigned to many platform numbers.
      </p>

      <div className="card">
        <table>
          <thead>
            <tr><th>Name</th><th>Status</th><th>Created</th><th></th></tr>
          </thead>
          <tbody>
            {(data || []).map((f) => (
              <tr key={f.id}>
                <td>{f.name}</td>
                <td>
                  {f.active_version_id
                    ? <span className="badge new">active</span>
                    : <span className="badge">draft</span>}
                </td>
                <td>{f.created_at ? new Date(f.created_at).toLocaleString() : "—"}</td>
                <td style={{ textAlign: "right" }}>
                  <button disabled title="The flow editor arrives in a later ticket">Open</button>
                </td>
              </tr>
            ))}
            {data && data.length === 0 && (
              <tr><td colSpan={4} className="muted" style={{ textAlign: "center", padding: 20 }}>
                No flows yet.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

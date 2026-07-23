import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";

type Flow = {
  id: string;
  name: string;
  active_version_id?: string | null;
  created_at?: string | null;
};

// Call Flows library. Lists flows from the Ticket-02 flows API (one flow → many numbers).
// "New flow" creates a flow and opens the rule-form authoring view (Ticket 08); "Open"
// opens an existing flow's latest version in that same form.
export default function Flows() {
  const qc = useQueryClient();
  const nav = useNavigate();
  const { data } = useQuery<Flow[]>({ queryKey: ["flows"], queryFn: api.flows });
  const [name, setName] = useState("");

  const create = useMutation({
    mutationFn: (n: string) => api.createFlow(n),
    onSuccess: (flow: Flow) => {
      qc.invalidateQueries({ queryKey: ["flows"] });
      nav(`/flows/${flow.id}`);
    },
  });

  const newFlow = () => {
    const n = name.trim();
    if (!n || create.isPending) return;
    create.mutate(n);
  };

  return (
    <div>
      <div className="toolbar" style={{ flexWrap: "wrap", gap: 8 }}>
        <h2 style={{ marginTop: 0, marginBottom: 0, flex: 1 }}>Call Flows</h2>
        <input
          placeholder="new flow name…"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && newFlow()}
        />
        <button className="primary" disabled={!name.trim() || create.isPending} onClick={newFlow}>
          New flow
        </button>
      </div>
      <p className="muted" style={{ marginTop: 4 }}>
        Reusable call-handling graphs. One flow can be assigned to many platform numbers.
      </p>

      <div className="card">
        <div className="tablewrap"><table>
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
                  <button onClick={() => nav(`/flows/${f.id}`)}>Open</button>
                </td>
              </tr>
            ))}
            {data && data.length === 0 && (
              <tr><td colSpan={4} className="muted" style={{ textAlign: "center", padding: 20 }}>
                No flows yet.
              </td></tr>
            )}
          </tbody>
        </table></div>
      </div>
    </div>
  );
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { requestDial } from "../lib/dialer";

export default function Callers() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [filters, setFilters] = useState<any>({ page: 1, page_size: 50 });
  const { data } = useQuery({ queryKey: ["callers", filters], queryFn: () => api.callers(filters) });
  const label = useMutation({
    mutationFn: ({ id, label }: any) => api.updateCaller(id, { label }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["callers"] }),
  });
  const set = (k: string, v: any) => setFilters((f: any) => ({ ...f, [k]: v, page: 1 }));

  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Callers</h2>
      <div className="toolbar">
        <input placeholder="search number…" onChange={(e) => set("q", e.target.value)} />
        <select onChange={(e) => set("is_new", e.target.value)}>
          <option value="">all</option>
          <option value="true">new (called once)</option>
          <option value="false">returning</option>
        </select>
      </div>
      <div className="card">
        <div className="tablewrap"><table>
          <thead>
            <tr><th>Number</th><th>Calls</th><th>First seen</th><th>Last seen</th><th>Spam score</th><th>Label</th><th></th></tr>
          </thead>
          <tbody>
            {(data?.items || []).map((c: any) => (
              <tr key={c.id}>
                <td>{c.phone_number}</td>
                <td>{c.total_calls}</td>
                <td>{c.first_seen_at ? new Date(c.first_seen_at).toLocaleDateString() : "—"}</td>
                <td>{c.last_seen_at ? new Date(c.last_seen_at).toLocaleDateString() : "—"}</td>
                <td>{c.spam_score != null ? Number(c.spam_score).toFixed(2) : "—"}</td>
                <td>
                  <select defaultValue={c.label || ""}
                          onChange={(e) => label.mutate({ id: c.id, label: e.target.value || null })}>
                    <option value="">—</option>
                    {["customer", "vendor", "known-spam", "other"].map((l) => <option key={l} value={l}>{l}</option>)}
                  </select>
                </td>
                <td>
                  {/* Ticket 14: outbound call action — prefills the platform dialer + jumps to it. */}
                  <button onClick={() => { requestDial(c.phone_number); navigate("/calls"); }}>Call</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table></div>
      </div>
    </div>
  );
}

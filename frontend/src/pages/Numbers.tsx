import { useQuery } from "@tanstack/react-query";
import { api } from "../api";

export default function Numbers() {
  const { data } = useQuery({ queryKey: ["numbers"], queryFn: api.numbers });
  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Numbers</h2>
      <div className="card">
        <table>
          <thead>
            <tr><th>Number</th><th>Friendly</th><th>Provider</th><th>Campaign</th><th>Forwards to</th><th>Calls</th><th>Last call</th></tr>
          </thead>
          <tbody>
            {(data || []).map((n: any) => (
              <tr key={n.id}>
                <td>{n.phone_number}</td>
                <td>{n.friendly_name || "—"}</td>
                <td><span className="badge prov">{n.provider}</span></td>
                <td>{n.campaign_name || "—"}</td>
                <td>{n.forwards_to || "—"}</td>
                <td>{n.total_calls}</td>
                <td>{n.last_call_at ? new Date(n.last_call_at).toLocaleString() : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

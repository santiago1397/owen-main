import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "../api";

export default function Numbers() {
  const { data } = useQuery({ queryKey: ["numbers"], queryFn: api.numbers });
  const [q, setQ] = useState("");

  const rows = useMemo(() => {
    const term = q.trim().toLowerCase();
    return (data || [])
      .filter((n: any) =>
        !term ||
        (n.friendly_name || "").toLowerCase().includes(term) ||
        (n.phone_number || "").toLowerCase().includes(term)
      )
      .sort((a: any, b: any) =>
        (a.friendly_name || a.phone_number || "").localeCompare(
          b.friendly_name || b.phone_number || "",
          undefined,
          { sensitivity: "base", numeric: true }
        )
      );
  }, [data, q]);

  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Numbers</h2>
      <div className="toolbar">
        <input
          placeholder="search number or name…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <span className="muted">{rows.length} of {data?.length ?? 0}</span>
      </div>
      <div className="card">
        <table>
          <thead>
            <tr><th>Number</th><th>Friendly</th><th>Provider</th><th>Campaign</th><th>Forwards to</th><th>Calls</th><th>Last call</th></tr>
          </thead>
          <tbody>
            {rows.map((n: any) => (
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

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { LifecycleBadge, type NumberRow, effectiveOwner, isPlatformManaged, providerPath } from "../lib/numbers";

// The Numbers hub is the operator's primary platform surface: one row per DID with its
// owner→media provider, flow, campaign, SMS-state and lifecycle. Rows open a detail view.
// Twilio/SignalWire (legacy attribution) rows are read-only — no management affordances.
export default function Numbers() {
  const { data } = useQuery<NumberRow[]>({ queryKey: ["numbers"], queryFn: api.numbers });
  const nav = useNavigate();
  const [q, setQ] = useState("");
  // Provider filter lives in the URL (?provider=bulkvs) so it survives the
  // list → detail → back loop and refreshes.
  const [params, setParams] = useSearchParams();
  const provider = (params.get("provider") || "").toLowerCase();

  // Effective-owner buckets with counts, derived from whatever the data holds
  // (ported DIDs count under their platform owner, not the legacy provider row).
  const providerCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const n of data || []) {
      const owner = effectiveOwner(n);
      counts.set(owner, (counts.get(owner) || 0) + 1);
    }
    return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [data]);

  const rows = useMemo(() => {
    const term = q.trim().toLowerCase();
    return (data || [])
      .filter((n) => !provider || effectiveOwner(n) === provider)
      .filter((n) =>
        !term ||
        (n.friendly_name || "").toLowerCase().includes(term) ||
        (n.phone_number || "").toLowerCase().includes(term)
      )
      .sort((a, b) =>
        (a.friendly_name || a.phone_number || "").localeCompare(
          b.friendly_name || b.phone_number || "",
          undefined,
          { sensitivity: "base", numeric: true }
        )
      );
  }, [data, q, provider]);

  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Numbers</h2>
      <p className="muted" style={{ marginTop: 4 }}>
        Manage every DID here. Platform (BulkVS/Asterisk) numbers can be assigned flows and
        SMS handling; attribution (Twilio/SignalWire) numbers are read-only.
      </p>
      <div className="toolbar">
        <input
          placeholder="search number or name…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select
          value={provider}
          onChange={(e) => {
            const v = e.target.value;
            setParams(v ? { provider: v } : {}, { replace: true });
          }}
        >
          <option value="">All providers ({data?.length ?? 0})</option>
          {providerCounts.map(([owner, count]) => (
            <option key={owner} value={owner}>{owner} ({count})</option>
          ))}
        </select>
        <span className="muted">{rows.length} of {data?.length ?? 0}</span>
      </div>
      <div className="card">
        <div className="tablewrap"><table>
          <thead>
            <tr>
              <th>Number</th><th>Friendly</th><th>Provider</th><th>Flow</th>
              <th>Campaign</th><th>SMS</th><th>Lifecycle</th><th>Calls</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((n) => {
              const managed = isPlatformManaged(n);
              return (
                <tr key={n.id} className="clickable" onClick={() => nav(`/numbers/${n.id}`)}>
                  <td>{n.phone_number}</td>
                  <td>{n.friendly_name || "—"}</td>
                  <td>
                    <span className="badge prov">{providerPath(n)}</span>
                    {!managed && <span className="muted" style={{ marginLeft: 6 }} title="Read-only attribution number">🔒</span>}
                  </td>
                  {/* Flow / SMS-state authoring is filled by later tickets; the columns exist
                      now so the hub's information architecture is complete. */}
                  <td className="muted">{managed ? "—" : "n/a"}</td>
                  <td>{n.campaign_name || "—"}</td>
                  <td className="muted">{managed ? "—" : "n/a"}</td>
                  <td>
                    <LifecycleBadge
                      lifecycle={n.lifecycle}
                      title={n.provider_status ? `Carrier status: ${n.provider_status}` : undefined}
                    />
                  </td>
                  <td>{n.total_calls}</td>
                </tr>
              );
            })}
            {data && rows.length === 0 && (
              <tr><td colSpan={8} className="muted" style={{ textAlign: "center", padding: 20 }}>
                No numbers.
              </td></tr>
            )}
          </tbody>
        </table></div>
      </div>
    </div>
  );
}

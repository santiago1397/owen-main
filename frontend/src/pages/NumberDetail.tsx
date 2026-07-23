import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, api } from "../api";
import { LifecycleBadge, type NumberRow, isCarrierActive, isPlatformManaged, providerPath } from "../lib/numbers";

type Flow = { id: string; name: string; active_version_id?: string | null };

// Ticket 15.5: assign/unassign a call flow on an Asterisk-managed number. The dropdown
// only offers flows WITH an active version (the backend guard 400s otherwise); the
// contract is PATCH /api/numbers/{id} with exactly {flow_id: string|null}.
function FlowAssignment({ n }: { n: NumberRow }) {
  const qc = useQueryClient();
  const { data: flows } = useQuery<Flow[]>({ queryKey: ["flows"], queryFn: api.flows });
  const [choice, setChoice] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  const patch = useMutation({
    mutationFn: (flow_id: string | null) => api.updateNumber(n.id, { flow_id }),
    onSuccess: () => {
      setError(null);
      setChoice("");
      qc.invalidateQueries({ queryKey: ["numbers"] });
    },
    onError: (e: any) => {
      // Guard failures come back as HTTP 400 with {"detail": "<message>"}.
      let msg = String(e?.message || e);
      if (e instanceof ApiError) {
        try {
          const detail = JSON.parse(e.message)?.detail;
          if (typeof detail === "string") msg = detail;
        } catch {
          /* leave raw message */
        }
      }
      setError(msg);
    },
  });

  const activeFlows = (flows || []).filter((f) => f.active_version_id);
  const assigned = (flows || []).find((f) => f.id === n.flow_id) || null;

  if ((n.media_provider || "").toLowerCase() !== "asterisk") {
    return (
      <p className="muted" style={{ margin: 0 }}>
        Flow assignment is available for Asterisk-managed numbers only.
      </p>
    );
  }

  return (
    <div>
      {n.flow_id ? (
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
          <span>
            Assigned to{" "}
            {assigned
              ? <Link to={`/flows/${assigned.id}`}>{assigned.name}</Link>
              : <code className="mono">{n.flow_id}</code>}
          </span>
          <button disabled={patch.isPending} onClick={() => patch.mutate(null)}>
            Unassign
          </button>
        </div>
      ) : (
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <select value={choice} onChange={(e) => setChoice(e.target.value)}>
            <option value="">select a flow…</option>
            {activeFlows.map((f) => (
              <option key={f.id} value={f.id}>{f.name}</option>
            ))}
          </select>
          <button
            className="primary"
            disabled={!choice || patch.isPending}
            onClick={() => patch.mutate(choice)}
          >
            Assign
          </button>
          {flows && activeFlows.length === 0 && (
            <span className="muted">No flows with an active version yet.</span>
          )}
        </div>
      )}
      {error && <p style={{ color: "var(--danger)", margin: "8px 0 0" }}>{error}</p>}
    </div>
  );
}

// Per-number detail view. The numbers API returns a list only, so we read from the same
// cached list and pick the row by id — no new backend endpoint needed (additive).
// Later tickets fill the SMS gate / fallback-forward / flow-authoring slots below.
export default function NumberDetail() {
  const { id } = useParams();
  const { data } = useQuery<NumberRow[]>({ queryKey: ["numbers"], queryFn: api.numbers });
  const n = (data || []).find((x) => x.id === id);

  if (!data) return <div className="muted">Loading…</div>;
  if (!n) {
    return (
      <div>
        <Link to="/numbers">← Numbers</Link>
        <div className="placeholder">Number not found.</div>
      </div>
    );
  }

  const managed = isPlatformManaged(n);

  return (
    <div>
      <Link to="/numbers">← Numbers</Link>
      <div className="toolbar" style={{ marginTop: 10 }}>
        <h2 style={{ margin: 0, flex: 1 }}>{n.friendly_name || n.phone_number}</h2>
        <LifecycleBadge
          lifecycle={n.lifecycle}
          title={n.provider_status ? `Carrier status: ${n.provider_status}` : undefined}
        />
      </div>

      {!isCarrierActive(n) && (
        <div className="card" style={{ marginBottom: 12 }}>
          <p className="muted" style={{ margin: 0 }}>
            ⏳ This number is still provisioning at the carrier (status:{" "}
            <strong>{n.provider_status}</strong>). It cannot place calls or send messages
            until BulkVS reports it Active.
          </p>
        </div>
      )}

      <div className="card" style={{ marginBottom: 12 }}>
        <div className="kv">
          <span className="muted">Number</span><span>{n.phone_number}</span>
          <span className="muted">Provider</span><span>{providerPath(n)}</span>
          <span className="muted">Campaign</span><span>{n.campaign_name || "—"}</span>
          <span className="muted">Forwards to</span><span>{n.forwards_to || "—"}</span>
          <span className="muted">Total calls</span><span>{n.total_calls}</span>
          <span className="muted">Last call</span>
          <span>{n.last_call_at ? new Date(n.last_call_at).toLocaleString() : "—"}</span>
        </div>
      </div>

      {!managed && (
        <div className="card">
          <p className="muted" style={{ margin: 0 }}>
            🔒 This is an attribution number (Twilio/SignalWire). It is read-only in the
            platform hub — flow authoring and SMS handling apply to BulkVS/Asterisk numbers only.
          </p>
        </div>
      )}

      {managed && (
        <>
          <div className="card" style={{ marginBottom: 12 }}>
            <div className="l" style={{ marginBottom: 8 }}>Call flow</div>
            <FlowAssignment n={n} />
          </div>
          <div className="card" style={{ marginBottom: 12 }}>
            <div className="l" style={{ marginBottom: 8 }}>SMS gate</div>
            <p className="muted" style={{ margin: 0 }}>Inbound SMS handling — coming soon.</p>
          </div>
          <div className="card">
            <div className="l" style={{ marginBottom: 8 }}>Fallback forward</div>
            <p className="muted" style={{ margin: 0 }}>
              Fallback destination when no flow answers — coming soon.
            </p>
          </div>
        </>
      )}
    </div>
  );
}

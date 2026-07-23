import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { LifecycleBadge, type NumberRow, isCarrierActive, isPlatformManaged, providerPath } from "../lib/numbers";

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
            <p className="muted" style={{ margin: 0 }}>
              Flow authoring for this number is coming in a later ticket.
            </p>
            <button disabled style={{ marginTop: 10 }}>Assign flow (coming soon)</button>
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

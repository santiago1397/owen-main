import { useQuery } from "@tanstack/react-query";
import { api } from "../api";

function Copy({ text }: { text: string }) {
  // copyrow: below 560px this stacks the URL above the button — see styles.css. Inert above.
  return (
    <span className="copyrow">
      <code className="mono">{text}</code>{" "}
      <button onClick={() => navigator.clipboard.writeText(text)}>copy</button>
    </span>
  );
}

function Provider({ name, p }: { name: string; p: any }) {
  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
        <b style={{ textTransform: "capitalize" }}>{name}</b>
        <span className={"badge " + (p.configured ? "new" : "")}>{p.configured ? "configured" : "not configured"}</span>
      </div>
      <div className="kv">
        <span className="muted">Status webhook</span><Copy text={p.status_webhook} />
        <span className="muted">Recording webhook</span><Copy text={p.recording_webhook} />
        {p.message_webhook && (
          <>
            <span className="muted">Message webhook</span><Copy text={p.message_webhook} />
          </>
        )}
      </div>
    </div>
  );
}

export default function Settings() {
  const { data } = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  if (!data) return <div>Loading…</div>;
  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Settings</h2>
      <p className="muted">Paste these URLs into the provider console for each tracking number.</p>
      <Provider name="twilio" p={data.providers.twilio} />
      <Provider name="signalwire" p={data.providers.signalwire} />
      <div className="card">
        <div className="l" style={{ marginBottom: 8 }}>Engines</div>
        <div className="kv">
          <span className="muted">Timezone</span><span>{data.business_tz}</span>
          <span className="muted">Transcription</span><span>{data.engines.transcription}</span>
          <span className="muted">Analysis</span><span>{data.engines.analysis} ({data.engines.analysis_model})</span>
        </div>
      </div>
      <div className="card" style={{ marginTop: 12 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <b>GoHighLevel</b>
          <span className={"badge " + (data.ghl?.inbound_relay_configured ? "new" : "")}>
            {data.ghl?.inbound_relay_configured ? "inbound relay on" : "inbound relay off"}
          </span>
        </div>
        <p className="muted" style={{ marginBottom: 0 }}>
          Inbound texts to the message webhook are relayed to GHL's inbound webhook. Receive-only — no outbound SMS.
        </p>
      </div>
    </div>
  );
}

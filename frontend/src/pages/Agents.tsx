import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api";

// AI Agents library (Ticket 11). Lists reusable voice agents, creates new ones, and edits an
// agent's config as a NEW immutable version (append-only, like flow versions). An agent is
// never bound to a number — a flow's ai_agent node references it by id.

type Agent = {
  id: string;
  name: string;
  active_version_id?: string | null;
  created_at?: string | null;
};
type AgentVersion = { id: string; agent_id: string; version: number; config: any; created_at?: string | null };
type AgentDetail = Agent & { versions: AgentVersion[] };

const ENGINES = ["dummy", "openai_realtime", "vapi", "diy"];
const TOOLS = ["transfer", "end_call", "capture_lead", "send_sms"];

const EMPTY_CONFIG = {
  persona: "",
  voice: "",
  greeting: "",
  model: "",
  engine: "dummy",
  knowledge: "",
  tools: {} as Record<string, boolean>,
  guardrails: { max_call_seconds: 300, max_silence_seconds: 10, model_tier: "standard" } as any,
};

export default function Agents() {
  const qc = useQueryClient();
  const { data: agents } = useQuery<Agent[]>({ queryKey: ["agents"], queryFn: api.agents });
  const [name, setName] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: (n: string) => api.createAgent(n),
    onSuccess: (a: Agent) => {
      qc.invalidateQueries({ queryKey: ["agents"] });
      setName("");
      setSelected(a.id);
    },
  });

  const newAgent = () => {
    const n = name.trim();
    if (!n || create.isPending) return;
    create.mutate(n);
  };

  return (
    <div>
      <div className="toolbar" style={{ flexWrap: "wrap", gap: 8 }}>
        <h2 style={{ marginTop: 0, marginBottom: 0, flex: 1 }}>AI Agents</h2>
        <input
          placeholder="new agent name…"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && newAgent()}
        />
        <button className="primary" disabled={!name.trim() || create.isPending} onClick={newAgent}>
          New agent
        </button>
      </div>
      <p className="muted" style={{ marginTop: 4 }}>
        Reusable conversational voice agents. Drop one into a call flow's AI-agent node — an
        agent is never tied to a number.
      </p>

      <div className="card">
        <table>
          <thead>
            <tr><th>Name</th><th>Status</th><th>Created</th><th></th></tr>
          </thead>
          <tbody>
            {(agents || []).map((a) => (
              <tr key={a.id}>
                <td>{a.name}</td>
                <td>
                  {a.active_version_id
                    ? <span className="badge new">active</span>
                    : <span className="badge">draft</span>}
                </td>
                <td>{a.created_at ? new Date(a.created_at).toLocaleString() : "—"}</td>
                <td style={{ textAlign: "right" }}>
                  <button onClick={() => setSelected(a.id)}>Configure</button>
                </td>
              </tr>
            ))}
            {agents && agents.length === 0 && (
              <tr><td colSpan={4} className="muted" style={{ textAlign: "center", padding: 20 }}>
                No agents yet.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      {selected && <AgentEditor agentId={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

function AgentEditor({ agentId, onClose }: { agentId: string; onClose: () => void }) {
  const qc = useQueryClient();
  const { data: detail } = useQuery<AgentDetail>({
    queryKey: ["agent", agentId],
    queryFn: () => api.agent(agentId),
  });
  const [config, setConfig] = useState<any>(EMPTY_CONFIG);
  const [status, setStatus] = useState<string>("");

  // Prefill from the latest saved version (the newest one authored), else the empty template.
  useEffect(() => {
    if (!detail) return;
    const versions = detail.versions || [];
    const latest = versions.length ? versions[versions.length - 1] : null;
    setConfig(latest ? { ...EMPTY_CONFIG, ...latest.config, tools: { ...latest.config?.tools }, guardrails: { ...EMPTY_CONFIG.guardrails, ...latest.config?.guardrails } } : EMPTY_CONFIG);
  }, [detail]);

  const save = useMutation({
    mutationFn: () => api.saveAgentVersion(agentId, config),
    onSuccess: async (v: AgentVersion) => {
      setStatus(`Saved version ${v.version}.`);
      await qc.invalidateQueries({ queryKey: ["agent", agentId] });
      // Activate the just-saved version (validation may reject with warnings/errors).
      try {
        const res: any = await api.activateAgentVersion(agentId, v.id);
        setStatus(`Saved & activated v${v.version}.` + (res.warnings?.length ? ` Warnings: ${res.warnings.join("; ")}` : ""));
        qc.invalidateQueries({ queryKey: ["agents"] });
        qc.invalidateQueries({ queryKey: ["agent", agentId] });
      } catch (e: any) {
        setStatus(`Saved v${v.version} (draft) — activation refused: ${e.message}`);
      }
    },
    onError: (e: any) => setStatus(`Save failed: ${e.message}`),
  });

  const set = (k: string, v: any) => setConfig((c: any) => ({ ...c, [k]: v }));
  const setGuard = (k: string, v: any) => setConfig((c: any) => ({ ...c, guardrails: { ...c.guardrails, [k]: v } }));
  const toggleTool = (t: string) => setConfig((c: any) => ({ ...c, tools: { ...c.tools, [t]: !c.tools?.[t] } }));

  if (!detail) return null;

  return (
    <div className="card" style={{ marginTop: 16 }}>
      <div className="toolbar" style={{ gap: 8 }}>
        <h3 style={{ margin: 0, flex: 1 }}>Configure “{detail.name}”</h3>
        <button onClick={onClose}>Close</button>
        <button className="primary" disabled={save.isPending} onClick={() => save.mutate()}>
          Save version
        </button>
      </div>

      <div className="formgrid" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 12 }}>
        <label>Persona
          <textarea rows={3} value={config.persona} onChange={(e) => set("persona", e.target.value)}
            placeholder="How the agent behaves / who it is" style={{ width: "100%" }} />
        </label>
        <label>In-context knowledge
          <textarea rows={3} value={config.knowledge} onChange={(e) => set("knowledge", e.target.value)}
            placeholder="Facts the agent can reference" style={{ width: "100%" }} />
        </label>
        <label>Greeting
          <input value={config.greeting} onChange={(e) => set("greeting", e.target.value)}
            placeholder="First thing the agent says" style={{ width: "100%" }} />
        </label>
        <label>Voice
          <input value={config.voice} onChange={(e) => set("voice", e.target.value)}
            placeholder="e.g. alloy" style={{ width: "100%" }} />
        </label>
        <label>Model
          <input value={config.model} onChange={(e) => set("model", e.target.value)}
            placeholder="e.g. gpt-4o-realtime" style={{ width: "100%" }} />
        </label>
        <label>Engine
          <select value={config.engine} onChange={(e) => set("engine", e.target.value)} style={{ width: "100%" }}>
            {ENGINES.map((en) => <option key={en} value={en}>{en}</option>)}
          </select>
        </label>
      </div>

      <div className="navsection" style={{ marginTop: 16 }}>Tools</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
        {TOOLS.map((t) => (
          <label key={t} style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <input type="checkbox" checked={!!config.tools?.[t]} onChange={() => toggleTool(t)} />
            {t}
          </label>
        ))}
      </div>

      <div className="navsection" style={{ marginTop: 16 }}>Guardrails</div>
      <div className="formgrid" style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12 }}>
        <label>Max call seconds
          <input type="number" value={config.guardrails?.max_call_seconds ?? ""}
            onChange={(e) => setGuard("max_call_seconds", Number(e.target.value))} style={{ width: "100%" }} />
        </label>
        <label>Max silence seconds
          <input type="number" value={config.guardrails?.max_silence_seconds ?? ""}
            onChange={(e) => setGuard("max_silence_seconds", Number(e.target.value))} style={{ width: "100%" }} />
        </label>
        <label>Model tier
          <input value={config.guardrails?.model_tier ?? ""}
            onChange={(e) => setGuard("model_tier", e.target.value)} style={{ width: "100%" }} />
        </label>
      </div>

      {status && <p className="muted" style={{ marginTop: 12 }}>{status}</p>}
    </div>
  );
}

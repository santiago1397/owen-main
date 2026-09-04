import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api";
import AgentMonitor from "../components/AgentMonitor";

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

// `owen_voice` is the ONE engine that holds a real conversation — it runs the cascaded
// STT -> LLM -> TTS pipeline in the owen-voice container. `dummy` is offline and answers
// nobody; `openai_realtime` was superseded (its audio bridge was never implemented) and
// vapi/diy are registered stubs that raise. Labelled rather than hidden, because silently
// dropping an engine an existing agent already uses would make its config unopenable.
const ENGINES: { value: string; label: string }[] = [
  { value: "owen_voice", label: "owen_voice — real conversation (use this)" },
  { value: "dummy", label: "dummy — offline, never speaks" },
  { value: "openai_realtime", label: "openai_realtime — superseded, does not run" },
  { value: "vapi", label: "vapi — not implemented" },
  { value: "diy", label: "diy — not implemented" },
];
const TOOLS = ["transfer", "end_call", "capture_lead", "send_sms"];

// OpenAI TTS voices. Free-text was a trap: a typo produced a silent agent with no error.
const VOICES = ["alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer", "verse"];

const TRANSFER_KINDS = ["number", "operator", "flow", "agent"];

const EMPTY_CONFIG = {
  persona: "",
  voice: "alloy",
  greeting: "",
  model: "gpt-4o-mini",
  engine: "owen_voice",
  knowledge: "",
  llm_base_url: "",
  tts_instructions: "",
  tools: {} as Record<string, boolean>,
  guardrails: { max_call_seconds: 300, max_silence_seconds: 30, model_tier: "standard" } as any,
  transfer_targets: {} as Record<string, { kind: string; target: string }>,
  custom_tools: [] as any[],
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

      {/* Live calls sit ABOVE the editor: when an agent is misbehaving you need to seize
          the call, not scroll past it to find the config. */}
      <AgentMonitor />

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
    setConfig(latest
      ? {
          ...EMPTY_CONFIG,
          ...latest.config,
          tools: { ...latest.config?.tools },
          guardrails: { ...EMPTY_CONFIG.guardrails, ...latest.config?.guardrails },
          transfer_targets: { ...latest.config?.transfer_targets },
          custom_tools: latest.config?.custom_tools || [],
        }
      : EMPTY_CONFIG);
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

  // --- transfer allowlist. The agent picks a NAME from this list and can never name a
  // number itself, which is what stops a caller talking it into dialling somewhere.
  const setTarget = (name: string, patch: any) =>
    setConfig((c: any) => ({
      ...c,
      transfer_targets: { ...c.transfer_targets, [name]: { ...c.transfer_targets?.[name], ...patch } },
    }));
  const renameTarget = (from: string, to: string) =>
    setConfig((c: any) => {
      const next = { ...c.transfer_targets };
      const v = next[from];
      delete next[from];
      next[to || from] = v;
      return { ...c, transfer_targets: next };
    });
  const dropTarget = (name: string) =>
    setConfig((c: any) => {
      const next = { ...c.transfer_targets };
      delete next[name];
      return { ...c, transfer_targets: next };
    });
  const addTarget = () => {
    let n = "destination";
    let i = 1;
    while (config.transfer_targets?.[n]) n = `destination_${++i}`;
    setTarget(n, { kind: "number", target: "" });
  };

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
          <select value={config.voice} onChange={(e) => set("voice", e.target.value)} style={{ width: "100%" }}>
            {VOICES.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        </label>
        <label>Model
          <input value={config.model} onChange={(e) => set("model", e.target.value)}
            placeholder="e.g. gpt-4o-mini, deepseek-chat, MiniMax-Text-01" style={{ width: "100%" }} />
        </label>
        <label>Engine
          <select value={config.engine} onChange={(e) => set("engine", e.target.value)} style={{ width: "100%" }}>
            {ENGINES.map((en) => <option key={en.value} value={en.value}>{en.label}</option>)}
          </select>
        </label>
        <label>LLM endpoint (optional)
          <input value={config.llm_base_url} onChange={(e) => set("llm_base_url", e.target.value)}
            placeholder="blank = OpenAI · https://api.minimax.io/v1 · a DeepSeek/Kimi host"
            style={{ width: "100%" }} />
        </label>
      </div>
      <p className="muted" style={{ marginTop: 4 }}>
        Any OpenAI-compatible endpoint works as the brain — OpenAI, MiniMax, DeepSeek, Kimi or
        an aggregator. Prefer a US/EU host: a China-hosted endpoint costs roughly 200–250&nbsp;ms
        per turn from this server.
      </p>
      <label style={{ display: "block", marginTop: 12 }}>Delivery direction (optional)
        <input value={config.tts_instructions} onChange={(e) => set("tts_instructions", e.target.value)}
          placeholder="e.g. warm and unhurried — leave blank unless you have tested it on a real call"
          style={{ width: "100%" }} />
      </label>
      <p className="muted" style={{ marginTop: 4 }}>
        Steers pace and tone, but only on the <code>gpt-4o-mini-tts</code> family. Strong style
        prompts make the model <em>perform</em>, and breathiness and wide dynamics are exactly
        what an 8&nbsp;kHz phone codec destroys — judge it down a real phone, never on speakers.
      </p>

      <div className="navsection" style={{ marginTop: 16 }}>Tools</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
        {TOOLS.map((t) => (
          <label key={t} style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <input type="checkbox" checked={!!config.tools?.[t]} onChange={() => toggleTool(t)} />
            {t}
          </label>
        ))}
      </div>

      <div className="navsection" style={{ marginTop: 16 }}>Transfer destinations</div>
      <p className="muted" style={{ marginTop: 0 }}>
        The agent picks a <strong>name</strong> from this list — it can never choose a number
        itself. That is deliberate: an agent able to dial anything could be talked into calling
        a premium-rate number. Leave empty and the agent can only exit through the flow's own
        <code> transfer</code> edge.
      </p>
      <table>
        <thead><tr><th>Name the agent says</th><th>Kind</th><th>Goes to</th><th /></tr></thead>
        <tbody>
          {Object.entries(config.transfer_targets || {}).map(([name, t]: any) => (
            <tr key={name}>
              <td>
                <input value={name} onChange={(e) => renameTarget(name, e.target.value)}
                  style={{ width: "100%" }} />
              </td>
              <td>
                <select value={t?.kind || "number"} onChange={(e) => setTarget(name, { kind: e.target.value })}>
                  {TRANSFER_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
                </select>
              </td>
              <td>
                <input value={t?.target || ""} onChange={(e) => setTarget(name, { target: e.target.value })}
                  placeholder={t?.kind === "operator" ? "operator email"
                    : t?.kind === "flow" ? "a DID whose flow should take over"
                    : t?.kind === "agent" ? "agent id" : "+1XXXXXXXXXX"}
                  style={{ width: "100%" }} />
              </td>
              <td><button onClick={() => dropTarget(name)}>Remove</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <button onClick={addTarget} style={{ marginTop: 8 }}>Add destination</button>

      <div className="navsection" style={{ marginTop: 16 }}>Custom tools (advanced)</div>
      <p className="muted" style={{ marginTop: 0 }}>
        HTTP calls this agent may make, as JSON. Reads can be <code>sync</code> (~800&nbsp;ms, the
        caller waits in silence); writes must be <code>async</code> — a lead being saved must never
        make someone wait. Validated when you save.
      </p>
      <textarea
        rows={5}
        style={{ width: "100%", fontFamily: "monospace" }}
        value={JSON.stringify(config.custom_tools || [], null, 2)}
        onChange={(e) => {
          try {
            set("custom_tools", JSON.parse(e.target.value || "[]"));
            setStatus("");
          } catch {
            setStatus("Custom tools: not valid JSON yet — fix before saving.");
          }
        }}
        placeholder={'[{"name":"lookup_job","url":"https://…","method":"GET","mode":"sync","description":"Look up a job by number","parameters":{"type":"object","properties":{"job_id":{"type":"string"}}}}]'}
      />

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

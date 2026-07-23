import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type NodeTypes,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, api } from "../api";
import FlowNodeCard from "../components/FlowNodeCard";
import FlowNodePanel from "../components/FlowNodePanel";
import {
  NODE_TITLES,
  PALETTE,
  type CanvasNode,
  type NodeType,
  canvasToGraph,
  emptyCanvas,
  graphToCanvas,
  makeEdge,
  makeNode,
  uniqueId,
  unwiredPorts,
} from "../lib/canvasGraph";
import { type NumberRow } from "../lib/numbers";

type FlowVersion = { id: string; version: number; graph: any; created_at?: string | null };
type FlowDetail = {
  id: string;
  name: string;
  active_version_id?: string | null;
  versions: FlowVersion[];
};

type Feedback = {
  kind: "errors" | "warnings" | "ok";
  errors: string[];
  warnings: string[];
  message?: string;
} | null;

const nodeTypes: NodeTypes = { flowNode: FlowNodeCard };

// Visual flow builder (Ticket 16). The React Flow canvas replaces the Ticket-08 rule form
// entirely: the operator drags nodes from the palette, wires validator ports as edges, and
// configures the selected node in the side panel. The canvas serializes to the exact
// Ticket-02 graph JSON (origin: "canvas", additive `layout` for positions). Save draft
// appends an immutable version; Activate saves then runs backend validation, surfacing
// hard errors (block) vs warnings (allow). The version history panel opens old versions
// read-only and can restore one as a new version.
export default function FlowEditor() {
  return (
    <ReactFlowProvider>
      <Editor />
    </ReactFlowProvider>
  );
}

function Editor() {
  const { id } = useParams();
  const qc = useQueryClient();
  const { data } = useQuery<FlowDetail>({ queryKey: ["flow", id], queryFn: () => api.flow(id!) });
  const { data: versions } = useQuery<FlowVersion[]>({
    queryKey: ["flowVersions", id],
    queryFn: () => api.flowVersions(id!),
  });
  const { data: agents } = useQuery<any[]>({ queryKey: ["agents"], queryFn: api.agents });
  const { data: numbers } = useQuery<NumberRow[]>({ queryKey: ["numbers"], queryFn: api.numbers });

  const [nodes, setNodes, onNodesChange] = useNodesState<CanvasNode>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const [defaultFallback, setDefaultFallback] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [savedVersion, setSavedVersion] = useState<number | null>(null);
  const [showVersions, setShowVersions] = useState(false);
  // Non-null while viewing an OLD version read-only (banner + editing disabled).
  const [viewing, setViewing] = useState<FlowVersion | null>(null);

  const rf = useReactFlow();
  const wrapRef = useRef<HTMLDivElement>(null);

  const loadGraph = useCallback(
    (graph: any) => {
      const state = graph ? graphToCanvas(graph) : emptyCanvas();
      setNodes(state.nodes);
      setEdges(state.edges);
      setDefaultFallback(state.defaultFallback);
      setSelectedId(null);
      window.setTimeout(() => rf.fitView({ padding: 0.15 }), 30);
    },
    [rf, setEdges, setNodes]
  );

  // Load the latest version's graph onto the canvas once.
  useEffect(() => {
    if (!data || loaded) return;
    const latest = data.versions[data.versions.length - 1];
    loadGraph(latest ? latest.graph : null);
    setLoaded(true);
  }, [data, loaded, loadGraph]);

  const readOnly = viewing != null;
  const graph = useMemo(
    () => canvasToGraph(nodes, edges, defaultFallback),
    [nodes, edges, defaultFallback]
  );
  const lint = useMemo(() => unwiredPorts(nodes, edges), [nodes, edges]);
  const selected = nodes.find((n) => n.id === selectedId) || null;
  const assignedNumbers = (numbers || []).filter((n) => n.flow_id === id);

  // --- canvas interactions ---

  const onConnect = useCallback(
    (c: Connection) => {
      if (!c.source || !c.target) return;
      const port = c.sourceHandle || "default";
      // One edge per output port: rewiring a port replaces its previous edge.
      setEdges((eds) => [
        ...eds.filter((e) => !(e.source === c.source && e.sourceHandle === port)),
        makeEdge(c.source, port, c.target),
      ]);
    },
    [setEdges]
  );

  const addNode = useCallback(
    (ntype: NodeType, position?: { x: number; y: number }) => {
      setNodes((nds) => {
        const nid = uniqueId(ntype, nds);
        const pos =
          position ||
          // Click-to-add: drop right of the current right-most node.
          {
            x: Math.max(40, ...nds.map((n) => n.position.x)) + 270,
            y: 120,
          };
        setSelectedId(nid);
        return [...nds, { ...makeNode(nid, ntype, pos, {}), selected: true }].map((n) =>
          n.id === nid ? n : { ...n, selected: false }
        );
      });
    },
    [setNodes]
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const ntype = e.dataTransfer.getData("application/flownode") as NodeType;
      if (!ntype || readOnly) return;
      addNode(ntype, rf.screenToFlowPosition({ x: e.clientX, y: e.clientY }));
    },
    [addNode, readOnly, rf]
  );

  const onNodesDelete = useCallback(
    (deleted: { id: string }[]) => {
      const gone = new Set(deleted.map((n) => n.id));
      if (selectedId && gone.has(selectedId)) setSelectedId(null);
      setDefaultFallback((fb) => (fb && gone.has(fb) ? null : fb));
    },
    [selectedId]
  );

  const patchConfig = useCallback(
    (nid: string, patch: Record<string, any>) => {
      setNodes((nds) =>
        nds.map((n) => {
          if (n.id !== nid) return n;
          const config = { ...n.data.config };
          for (const [k, v] of Object.entries(patch)) {
            if (v === undefined) delete config[k];
            else config[k] = v;
          }
          return { ...n, data: { ...n.data, config } };
        })
      );
      // Disabling a menu digit must drop that port's edge too.
      if ("digits" in patch && Array.isArray(patch.digits)) {
        const keep = new Set([...patch.digits, "timeout", "invalid"]);
        setEdges((eds) =>
          eds.filter((e) => e.source !== nid || keep.has(e.sourceHandle || "default"))
        );
      }
    },
    [setEdges, setNodes]
  );

  const deleteNode = useCallback(
    (nid: string) => {
      setNodes((nds) => nds.filter((n) => n.id !== nid));
      setEdges((eds) => eds.filter((e) => e.source !== nid && e.target !== nid));
      onNodesDelete([{ id: nid }]);
    },
    [onNodesDelete, setEdges, setNodes]
  );

  // --- save / activate / versions ---

  async function saveDraft(g: any = graph): Promise<string | null> {
    setBusy(true);
    setFeedback(null);
    try {
      const v = await api.saveFlowVersion(id!, g);
      setSavedVersion(v.version);
      qc.invalidateQueries({ queryKey: ["flow", id] });
      qc.invalidateQueries({ queryKey: ["flowVersions", id] });
      qc.invalidateQueries({ queryKey: ["flows"] });
      return v.id;
    } catch (e: any) {
      setFeedback({ kind: "errors", errors: [`Save failed: ${e?.message || e}`], warnings: [] });
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function activate() {
    const versionId = await saveDraft();
    if (!versionId) return;
    setBusy(true);
    try {
      const res = await api.activateFlowVersion(id!, versionId);
      setFeedback({
        kind: "ok",
        errors: [],
        warnings: res.warnings || [],
        message: "Flow activated.",
      });
      qc.invalidateQueries({ queryKey: ["flow", id] });
      qc.invalidateQueries({ queryKey: ["flows"] });
    } catch (e: any) {
      // The flows API refuses hard-error activation with HTTP 400 whose body is
      // {"detail": {errors, warnings}} — ApiError.message carries that raw JSON text.
      let errors = [String(e?.message || e)];
      let warnings: string[] = [];
      if (e instanceof ApiError) {
        try {
          const detail = JSON.parse(e.message)?.detail;
          if (detail && Array.isArray(detail.errors)) {
            errors = detail.errors;
            warnings = detail.warnings || [];
          }
        } catch {
          /* leave raw message */
        }
      }
      setFeedback({ kind: "errors", errors, warnings });
    } finally {
      setBusy(false);
    }
  }

  const latestVersion = versions && versions.length ? versions[versions.length - 1] : null;

  function openVersion(v: FlowVersion) {
    if (latestVersion && v.id === latestVersion.id) {
      backToLatest();
      return;
    }
    setViewing(v);
    loadGraph(v.graph);
  }

  function backToLatest() {
    setViewing(null);
    loadGraph(latestVersion ? latestVersion.graph : null);
  }

  async function restoreVersion(v: FlowVersion) {
    const versionId = await saveDraft(v.graph);
    if (!versionId) return;
    setViewing(null);
    loadGraph(v.graph); // switch to editing the restored graph
    setFeedback({
      kind: "ok",
      errors: [],
      warnings: [],
      message: `Restored v${v.version} as a new draft version.`,
    });
  }

  if (!data) return <div className="muted">Loading…</div>;

  return (
    <div className="floweditor">
      <Link to="/flows">← Call Flows</Link>
      <div className="toolbar" style={{ marginTop: 10 }}>
        <h2 style={{ margin: 0 }}>{data.name}</h2>
        {data.active_version_id ? (
          <span className="badge new">active</span>
        ) : (
          <span className="badge">draft</span>
        )}
        <div style={{ flex: 1 }} />
        <label className="muted" style={{ display: "flex", alignItems: "center", gap: 6 }}>
          default fallback
          <select
            disabled={readOnly}
            value={defaultFallback || ""}
            onChange={(e) => setDefaultFallback(e.target.value || null)}
          >
            <option value="">— none —</option>
            {nodes
              .filter((n) => n.data.ntype !== "entry")
              .map((n) => (
                <option key={n.id} value={n.id}>
                  {NODE_TITLES[n.data.ntype]} · {n.data.config.label || n.id}
                </option>
              ))}
          </select>
        </label>
        <button
          className={showVersions ? "primary" : ""}
          onClick={() => setShowVersions((s) => !s)}
        >
          Versions
        </button>
        <button disabled={busy || readOnly} onClick={() => saveDraft()}>
          Save draft
        </button>
        <button className="primary" disabled={busy || readOnly} onClick={activate}>
          Activate
        </button>
      </div>

      {viewing && (
        <div className="card ro-banner">
          <span>
            Viewing <b>v{viewing.version}</b> — read only.
          </span>
          <button onClick={() => restoreVersion(viewing)} disabled={busy}>
            Restore as new version
          </button>
          <button onClick={backToLatest}>Back to latest</button>
        </div>
      )}

      {feedback && (
        <div className="card" style={{ marginBottom: 10, padding: 12 }}>
          {feedback.kind === "ok" && (
            <div style={{ color: "var(--good)", fontWeight: 600 }}>
              ✓ {feedback.message || "Saved."}
            </div>
          )}
          {feedback.errors.length > 0 && (
            <div style={{ marginBottom: feedback.warnings.length ? 10 : 0 }}>
              <div style={{ color: "var(--danger)", fontWeight: 600, marginBottom: 4 }}>
                Errors (must fix before activating)
              </div>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {feedback.errors.map((e, i) => (
                  <li key={i} style={{ color: "var(--danger)" }}>
                    {e}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {feedback.warnings.length > 0 && (
            <div>
              <span className="badge" style={{ color: "var(--warn)", borderColor: "var(--warn)" }}>
                warnings
              </span>
              <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                {feedback.warnings.map((w, i) => (
                  <li key={i} style={{ color: "var(--warn)" }}>
                    {w}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      <div className="flowgrid">
        {/* Node palette */}
        <div className="card flowpalette">
          <div className="l" style={{ marginBottom: 8 }}>
            Nodes
          </div>
          {PALETTE.map((p) => (
            <div
              key={p.type}
              className="palettenode"
              draggable={!readOnly}
              title={p.hint + " — drag onto the canvas or click to add"}
              onDragStart={(e) => e.dataTransfer.setData("application/flownode", p.type)}
              onClick={() => !readOnly && addNode(p.type)}
            >
              <span className={`fn-type fn-type-${p.type}`}>{p.title}</span>
              <span className="muted palettehint">{p.hint}</span>
            </div>
          ))}
          {lint.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <span className="badge" style={{ color: "var(--warn)", borderColor: "var(--warn)" }}>
                {lint.length} unwired port{lint.length > 1 ? "s" : ""}
              </span>
              <ul className="muted" style={{ margin: "6px 0 0", paddingLeft: 16, fontSize: 12 }}>
                {lint.slice(0, 6).map((w) => (
                  <li key={w}>{w}</li>
                ))}
                {lint.length > 6 && <li>…and {lint.length - 6} more</li>}
              </ul>
            </div>
          )}
        </div>

        {/* Canvas */}
        <div className="flowcanvas card" ref={wrapRef} onDrop={onDrop} onDragOver={(e) => e.preventDefault()}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodesDelete={onNodesDelete}
            onSelectionChange={({ nodes: sel }) => setSelectedId(sel[0]?.id || null)}
            nodesDraggable={!readOnly}
            nodesConnectable={!readOnly}
            edgesReconnectable={!readOnly}
            deleteKeyCode={readOnly ? null : ["Backspace", "Delete"]}
            colorMode="dark"
            fitView
            proOptions={{ hideAttribution: true }}
            defaultEdgeOptions={{ style: { strokeWidth: 1.5 } }}
          >
            <Background gap={18} />
            <Controls showInteractive={false} />
            <MiniMap pannable zoomable />
          </ReactFlow>
        </div>

        {/* Right panel: version history or the selected node's config */}
        <div className="card flowside">
          {showVersions ? (
            <VersionList
              versions={versions || data.versions}
              activeId={data.active_version_id || null}
              viewingId={viewing?.id || null}
              onOpen={openVersion}
              onRestore={restoreVersion}
              busy={busy}
            />
          ) : selected ? (
            <FlowNodePanel
              node={selected}
              agents={agents || []}
              readOnly={readOnly}
              onChange={(patch) => patchConfig(selected.id, patch)}
              onDelete={() => deleteNode(selected.id)}
            />
          ) : (
            <div className="muted" style={{ fontSize: 13 }}>
              Select a node to configure it, or add one from the palette.
              {savedVersion != null && (
                <p style={{ marginTop: 10 }}>Last saved as version {savedVersion}.</p>
              )}
            </div>
          )}

          {assignedNumbers.length > 0 && (
            <div style={{ marginTop: 18, borderTop: "1px solid var(--border)", paddingTop: 12 }}>
              <div className="l" style={{ marginBottom: 6 }}>
                Assigned numbers
              </div>
              {assignedNumbers.map((n) => (
                <div key={n.id} style={{ marginBottom: 4, fontSize: 13 }}>
                  <Link to={`/numbers/${n.id}`}>{n.friendly_name || n.phone_number}</Link>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function VersionList({
  versions,
  activeId,
  viewingId,
  onOpen,
  onRestore,
  busy,
}: {
  versions: FlowVersion[];
  activeId: string | null;
  viewingId: string | null;
  onOpen: (v: FlowVersion) => void;
  onRestore: (v: FlowVersion) => void;
  busy: boolean;
}) {
  const latest = versions[versions.length - 1];
  const list = [...versions].reverse();
  return (
    <div>
      <div className="l" style={{ marginBottom: 8 }}>
        Version history
      </div>
      {list.length === 0 && <div className="muted">No versions saved yet.</div>}
      {list.map((v) => {
        const isLatest = latest && v.id === latest.id;
        const isViewing = viewingId === v.id || (isLatest && !viewingId);
        return (
          <div key={v.id} className={"versionrow" + (isViewing ? " sel" : "")}>
            <button className="versionopen" onClick={() => onOpen(v)}>
              v{v.version}
            </button>
            <span className="muted" style={{ flex: 1, fontSize: 12 }}>
              {v.created_at ? new Date(v.created_at).toLocaleString() : ""}
            </span>
            {v.id === activeId && <span className="badge new">active</span>}
            {isLatest && <span className="badge">latest</span>}
            {!isLatest && (
              <button disabled={busy} onClick={() => onRestore(v)} title="Save this graph as a new version and edit it">
                Restore
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}

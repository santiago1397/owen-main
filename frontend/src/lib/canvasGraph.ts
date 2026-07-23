// Canvas ↔ graph translation for the visual flow builder (Ticket 16).
//
// The canvas state (React Flow nodes + edges) serializes to the EXACT Ticket-02 graph JSON
// that backend/app/flows/validator.py accepts:
//   { origin, default_fallback, nodes: { <id>: { type, next: { <port>: <target-id> }, ...config } } }
// plus an ADDITIVE `layout` key ({node_id: {x, y}}) that the validator/interpreter ignore.
//
// Port sets per node type mirror validator.ALLOWED_PORTS exactly. `record` is a MODIFIER
// flag on play/dial nodes (never its own node type). ai_agent includes `failed` (aligned
// with the engine per Ticket 15.4).

import type { Edge, Node } from "@xyflow/react";

export const ORIGIN = "canvas";

export type NodeType =
  | "entry"
  | "play"
  | "hours"
  | "menu"
  | "dial"
  | "voicemail"
  | "ai_agent"
  | "hangup"
  // Ticket 17 parity nodes.
  | "set_vars"
  | "unset_vars"
  | "conditions"
  | "send_sms"
  | "request";

// Ticket 17 conditions node operators (mirror validator/variables CONDITION_OPERATORS).
export const CONDITION_OPERATORS = [
  "equals",
  "not_equals",
  "contains",
  "regex",
  "gt",
  "lt",
  "is_empty",
] as const;
export type ConditionOperator = (typeof CONDITION_OPERATORS)[number];

export type ConditionRow = {
  variable: string;
  operator: ConditionOperator;
  value: string;
  port: string;
};

export const MENU_DIGITS = ["0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "#"];

export const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
export type Day = (typeof DAYS)[number];

// Everything on a node besides `type` and `next` — free-form config + modifiers.
export type NodeConfig = Record<string, any>;

export type FlowNodeData = {
  ntype: NodeType;
  config: NodeConfig;
  // Read-only rendering hint (viewing an old version).
  readOnly?: boolean;
};

export type CanvasNode = Node<FlowNodeData>;

// Palette entries — everything but `entry` (which always exists, exactly once).
export const PALETTE: { type: NodeType; title: string; hint: string }[] = [
  { type: "play", title: "Play", hint: "Speak a prompt (TTS); optional record" },
  { type: "hours", title: "Hours", hint: "Business-hours branch (open / closed)" },
  { type: "menu", title: "Menu", hint: "DTMF keypad menu" },
  { type: "dial", title: "Dial", hint: "Forward to a number or operator" },
  { type: "voicemail", title: "Voicemail", hint: "Greeting + record a message (terminal)" },
  { type: "ai_agent", title: "AI Agent", hint: "Hand the call to a voice agent" },
  { type: "set_vars", title: "Set vars", hint: "Assign variables (literals or {{vars}})" },
  { type: "unset_vars", title: "Unset vars", hint: "Remove variables" },
  { type: "conditions", title: "Conditions", hint: "Branch on variable comparisons" },
  { type: "send_sms", title: "Send SMS", hint: "Fire-and-forget SMS from this DID" },
  { type: "request", title: "HTTP request", hint: "Call an API; branch on success/failure" },
  { type: "hangup", title: "Hang up", hint: "End the call (terminal)" },
];

export const NODE_TITLES: Record<NodeType, string> = {
  entry: "Entry",
  play: "Play",
  hours: "Hours",
  menu: "Menu",
  dial: "Dial",
  voicemail: "Voicemail",
  ai_agent: "AI Agent",
  hangup: "Hang up",
  set_vars: "Set vars",
  unset_vars: "Unset vars",
  conditions: "Conditions",
  send_sms: "Send SMS",
  request: "HTTP request",
};

// Mirrors validator.ALLOWED_PORTS. Menu ports (and conditions ports) are computed from
// config (see nodePorts / conditionPorts).
const STATIC_PORTS: Record<Exclude<NodeType, "menu" | "conditions">, string[]> = {
  entry: ["default"],
  play: ["default"],
  hours: ["open", "closed"],
  dial: ["answered", "noanswer", "busy", "failed"],
  voicemail: ["default"],
  // `failed` is being added backend-side (Ticket 15.4) — include it here.
  ai_agent: ["default", "transfer", "complete", "failed"],
  hangup: [],
  // Ticket 17 parity nodes.
  set_vars: ["default"],
  unset_vars: ["default"],
  send_sms: ["default"],
  request: ["success", "failure"],
};

// A conditions node's dynamic ports: each row's port (in order) + the required "else"
// (mirrors validator.condition_ports). Per-row ports default to match_<n> — see rowPortName.
export function conditionRows(config: NodeConfig): ConditionRow[] {
  return Array.isArray(config.rows) ? (config.rows as ConditionRow[]) : [];
}

export function rowPortName(index: number): string {
  return `match_${index + 1}`;
}

export function conditionPorts(config: NodeConfig): string[] {
  const rows = conditionRows(config);
  const ports = rows.map((r, i) => r.port || rowPortName(i));
  return [...ports, "else"];
}

// The enabled digits of a menu node: explicit `digits` config, else derived from wiring.
export function menuDigits(config: NodeConfig, next?: Record<string, string>): string[] {
  const explicit = Array.isArray(config.digits) ? config.digits.map(String) : null;
  if (explicit) return MENU_DIGITS.filter((d) => explicit.includes(d));
  const wired = next ? Object.keys(next) : [];
  return MENU_DIGITS.filter((d) => wired.includes(d));
}

// The output ports (source handles) a node of this type/config exposes, in render order.
export function nodePorts(ntype: NodeType, config: NodeConfig): string[] {
  if (ntype === "menu") return [...menuDigits(config), "timeout", "invalid"];
  if (ntype === "conditions") return conditionPorts(config);
  return STATIC_PORTS[ntype] || [];
}

// One-line human summary of a node's key config, for the on-canvas card.
export function nodeSummary(ntype: NodeType, config: NodeConfig): string {
  switch (ntype) {
    case "entry":
      return "call starts here";
    case "play": {
      const p = (config.prompt || "").trim();
      const rec = config.record ? " · rec" : "";
      return (p ? `“${p.length > 48 ? p.slice(0, 48) + "…" : p}”` : "no prompt") + rec;
    }
    case "hours": {
      const sched = config.hours?.schedule || {};
      const days = DAYS.filter((d) => (sched[d] || []).length > 0);
      return days.length ? days.join(" ") : "no schedule (always open)";
    }
    case "menu": {
      const digits = menuDigits(config);
      return digits.length ? `digits ${digits.join(" ")}` : "no digits enabled";
    }
    case "dial": {
      if ((config.target_kind || config.kind) === "operator") {
        const ops = Array.isArray(config.operators) ? config.operators : [];
        return `operator${ops.length > 1 ? "s" : ""}: ${ops.join(", ") || "—"}`;
      }
      return config.target || config.number || "no target";
    }
    case "voicemail": {
      const g = (config.greeting || config.prompt || "").trim();
      return g ? `“${g.length > 48 ? g.slice(0, 48) + "…" : g}”` : "no greeting";
    }
    case "ai_agent":
      return config.agent_name || config.agent_id || "no agent selected";
    case "set_vars": {
      const names = Object.keys(config.vars || {});
      return names.length ? `set ${names.join(", ")}` : "no vars set";
    }
    case "unset_vars": {
      const names = Array.isArray(config.names) ? config.names : [];
      return names.length ? `unset ${names.join(", ")}` : "no vars";
    }
    case "conditions": {
      const rows = conditionRows(config);
      return rows.length ? `${rows.length} rule${rows.length > 1 ? "s" : ""} + else` : "no rules (else only)";
    }
    case "send_sms": {
      const to = (config.to || "{{caller_number}}").trim();
      const b = (config.body || "").trim();
      return `→ ${to}${b ? ` · “${b.length > 32 ? b.slice(0, 32) + "…" : b}”` : ""}`;
    }
    case "request": {
      const m = (config.method || "GET").toUpperCase();
      const u = (config.url || "").trim();
      return u ? `${m} ${u.length > 40 ? u.slice(0, 40) + "…" : u}` : `${m} (no url)`;
    }
    case "hangup":
      return "ends the call";
  }
}

// --- graph JSON -> canvas -----------------------------------------------------------------

export type CanvasState = {
  nodes: CanvasNode[];
  edges: Edge[];
  defaultFallback: string | null;
};

export function emptyCanvas(): CanvasState {
  return {
    nodes: [makeNode("entry", "entry", { x: 40, y: 120 }, {})],
    edges: [],
    defaultFallback: null,
  };
}

export function makeNode(
  id: string,
  ntype: NodeType,
  position: { x: number; y: number },
  config: NodeConfig
): CanvasNode {
  return {
    id,
    type: "flowNode",
    position,
    data: { ntype, config },
    deletable: ntype !== "entry", // exactly one entry, never deletable
  };
}

const edgeId = (source: string, port: string) => `${source}__${port}`;

export function makeEdge(source: string, port: string, target: string): Edge {
  return {
    id: edgeId(source, port),
    source,
    sourceHandle: port,
    target,
    label: port === "default" ? undefined : port,
  };
}

export function graphToCanvas(graph: any): CanvasState {
  const gnodes: Record<string, any> =
    graph && typeof graph.nodes === "object" && graph.nodes ? graph.nodes : {};
  const ids = Object.keys(gnodes);
  if (ids.length === 0) return emptyCanvas();

  const layout: Record<string, { x: number; y: number }> =
    graph.layout && typeof graph.layout === "object" ? graph.layout : {};
  const havePositions = ids.some(
    (id) => layout[id] && typeof layout[id].x === "number" && typeof layout[id].y === "number"
  );
  const positions = havePositions ? layout : autoLayout(gnodes);

  const nodes: CanvasNode[] = [];
  const edges: Edge[] = [];
  for (const id of ids) {
    const raw = gnodes[id] || {};
    const { type, next, ...config } = raw;
    const ntype: NodeType = (type as NodeType) || "play";
    // Old graphs have no explicit menu `digits` — derive them from wiring so handles render.
    if (ntype === "menu" && !Array.isArray(config.digits)) {
      config.digits = menuDigits({}, next || {});
    }
    const pos = positions[id] || { x: 40, y: 40 };
    nodes.push(makeNode(id, ntype, { x: pos.x, y: pos.y }, config));
    if (next && typeof next === "object") {
      for (const [port, target] of Object.entries(next)) {
        if (typeof target === "string" && target in gnodes) {
          edges.push(makeEdge(id, port, target));
        }
      }
    }
  }

  // Guarantee the invariant "exactly one entry node exists on the canvas".
  if (!nodes.some((n) => n.data.ntype === "entry")) {
    nodes.unshift(makeNode(uniqueId("entry", nodes), "entry", { x: 40, y: 40 }, {}));
  }

  const fb = graph.default_fallback;
  return {
    nodes,
    edges,
    defaultFallback: typeof fb === "string" && fb in gnodes ? fb : null,
  };
}

// --- canvas -> graph JSON -----------------------------------------------------------------

// Drop empty-string / undefined / null config values so the stored graph stays clean.
function cleanConfig(config: NodeConfig): NodeConfig {
  const out: NodeConfig = {};
  for (const [k, v] of Object.entries(config)) {
    if (v === undefined || v === null) continue;
    if (typeof v === "string" && v.trim() === "") continue;
    if (Array.isArray(v) && v.length === 0 && k !== "digits") continue;
    out[k] = v;
  }
  return out;
}

export function canvasToGraph(
  nodes: CanvasNode[],
  edges: Edge[],
  defaultFallback: string | null
): any {
  const gnodes: Record<string, any> = {};
  const layout: Record<string, { x: number; y: number }> = {};
  const nodeIds = new Set(nodes.map((n) => n.id));

  for (const n of nodes) {
    const next: Record<string, string> = {};
    for (const e of edges) {
      if (e.source === n.id && e.sourceHandle && nodeIds.has(e.target)) {
        next[e.sourceHandle] = e.target;
      }
    }
    gnodes[n.id] = { type: n.data.ntype, ...cleanConfig(n.data.config), next };
    layout[n.id] = { x: Math.round(n.position.x), y: Math.round(n.position.y) };
  }

  const graph: any = { origin: ORIGIN, nodes: gnodes, layout };
  if (defaultFallback && nodeIds.has(defaultFallback)) graph.default_fallback = defaultFallback;
  return graph;
}

// --- helpers ------------------------------------------------------------------------------

export function uniqueId(base: string, nodes: { id: string }[]): string {
  const taken = new Set(nodes.map((n) => n.id));
  if (!taken.has(base)) return base;
  let i = 2;
  while (taken.has(`${base}_${i}`)) i++;
  return `${base}_${i}`;
}

// Light client-side lint (server stays the authority on activate): list unwired ports that
// the validator would warn about, so the operator sees them before saving.
export function unwiredPorts(nodes: CanvasNode[], edges: Edge[]): string[] {
  const wired = new Set(edges.map((e) => `${e.source}__${e.sourceHandle}`));
  const out: string[] = [];
  for (const n of nodes) {
    for (const port of nodePorts(n.data.ntype, n.data.config)) {
      if (!wired.has(`${n.id}__${port}`)) out.push(`${n.id} · ${port}`);
    }
  }
  return out;
}

// --- tiny built-in layered auto-layout (LR) -----------------------------------------------
// For graphs saved before the canvas existed (no `layout` key): BFS from the entry node
// (plus any unreached nodes in a trailing column), one column per depth layer.

const COL_W = 270;
const ROW_H = 150;

function autoLayout(gnodes: Record<string, any>): Record<string, { x: number; y: number }> {
  const ids = Object.keys(gnodes);
  const depth: Record<string, number> = {};
  const entry = ids.find((id) => gnodes[id]?.type === "entry");

  const queue: string[] = [];
  if (entry) {
    depth[entry] = 0;
    queue.push(entry);
  }
  while (queue.length) {
    const cur = queue.shift()!;
    const next = gnodes[cur]?.next;
    if (next && typeof next === "object") {
      for (const target of Object.values(next)) {
        if (typeof target === "string" && target in gnodes && !(target in depth)) {
          depth[target] = depth[cur] + 1;
          queue.push(target);
        }
      }
    }
  }
  const maxDepth = Math.max(0, ...Object.values(depth));
  for (const id of ids) if (!(id in depth)) depth[id] = maxDepth + 1; // unreached column

  const perLayerCount: Record<number, number> = {};
  const positions: Record<string, { x: number; y: number }> = {};
  for (const id of ids) {
    const d = depth[id];
    const row = perLayerCount[d] || 0;
    perLayerCount[d] = row + 1;
    positions[id] = { x: 40 + d * COL_W, y: 40 + row * ROW_H };
  }
  return positions;
}

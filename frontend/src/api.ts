import { setActiveCall } from "./lib/activeCall";
import { clearOutboundIntent, markOutboundIntent } from "./lib/outboundIntent";

export const API_BASE = (import.meta as any).env?.VITE_API_BASE || "";

const TOKEN_KEY = "callmon_token";
const REFRESH_KEY = "callmon_refresh";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(t: string) {
  localStorage.setItem(TOKEN_KEY, t);
}
function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY);
}
/** Persist BOTH halves of the pair. The access token is short-lived (30 min); the refresh
 *  token is the one that keeps a phone signed in across days. */
function setTokens(access: string, refresh?: string) {
  setToken(access);
  if (refresh) localStorage.setItem(REFRESH_KEY, refresh);
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

/** Flat rather than a discriminated union: this project compiles with `strict: false`,
 *  where narrowing on a literal `ok` discriminant is unreliable. */
type RefreshResult = { ok: boolean; token?: string; reason?: "rejected" | "network" };

let refreshing: Promise<RefreshResult> | null = null;

/** Single-flight refresh: if several requests 401 at once (the dashboard fires many in
 *  parallel), they all await ONE /refresh call instead of stampeding it — and, since the
 *  backend rotates the refresh token on every use, racing calls would otherwise redeem a
 *  token that a sibling call had already replaced. */
function refreshAccessToken(): Promise<RefreshResult> {
  if (!refreshing) {
    const p = (async (): Promise<RefreshResult> => {
      const rt = getRefreshToken();
      if (!rt) return { ok: false, reason: "rejected" as const };
      try {
        const res = await fetch(`${API_BASE}/api/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: rt }),
        });
        // Only a definitive answer from the server ends the session. A 5xx is the server's
        // problem, not proof the token is bad, so it must not sign the user out.
        if (!res.ok) {
          return { ok: false, reason: res.status >= 500 ? ("network" as const) : ("rejected" as const) };
        }
        const data = await res.json();
        setTokens(data.access_token, data.refresh_token);
        return { ok: true, token: data.access_token as string };
      } catch {
        // Phone dropped signal mid-refresh. Keep the tokens — retrying later may well work.
        return { ok: false, reason: "network" as const };
      }
    })();
    refreshing = p;
    void p.finally(() => {
      if (refreshing === p) refreshing = null;
    });
  }
  return refreshing;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** FastAPI errors are `{"detail": ...}`. These strings are shown to the operator verbatim (the
 *  Inbox composer and the softphone sheet both print them), so unwrap `detail` instead of
 *  surfacing raw JSON. Anything unexpected falls back to the body as-is. */
async function errorMessage(res: Response): Promise<string> {
  const text = await res.text();
  try {
    const detail = JSON.parse(text)?.detail;
    if (typeof detail === "string" && detail) return detail;
  } catch {
    /* not JSON — use the body */
  }
  return text;
}

async function request(path: string, opts: RequestInit = {}, allowRetry = true): Promise<any> {
  const headers: Record<string, string> = { ...(opts.headers as any) };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers });
  if (res.status === 401) {
    // The access token lasts 30 min; the refresh token lasts far longer and is rotated on
    // every use. So an expired access token is a silent, invisible refresh — NOT a logout.
    if (allowRetry) {
      const r = await refreshAccessToken();
      if (r.ok) return request(path, opts, false);
      if (r.reason === "network") throw new ApiError(401, "could not reach the server");
    }
    clearToken();
    if (!location.pathname.startsWith("/login")) location.href = "/login";
    throw new ApiError(401, "unauthorized");
  }
  if (!res.ok) throw new ApiError(res.status, await errorMessage(res));
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

export async function login(email: string, password: string): Promise<string> {
  const body = new URLSearchParams({ username: email, password });
  const res = await fetch(`${API_BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) throw new ApiError(res.status, "login failed");
  const data = await res.json();
  // Keep the refresh token too — it is what spares the user a daily re-login on mobile.
  setTokens(data.access_token, data.refresh_token);
  return data.access_token;
}

const qs = (params: Record<string, any>) => {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  });
  const s = p.toString();
  return s ? `?${s}` : "";
};

export const api = {
  me: () => request("/api/auth/me"),
  dashboard: (params: { date_from?: string; date_to?: string; hide_junk?: boolean }) =>
    request(`/api/dashboard/summary${qs(params)}`),
  // --- Campaign attribution (/attribution) ---
  // Both endpoints are READ-ONLY. `match` is a POST only because the phone list is too long
  // for a query string; job/revenue data is never sent to or stored on the server.
  attributionCampaigns: (params: { date_from?: string; date_to?: string; min_duration_seconds?: number }) =>
    request(`/api/attribution/campaigns${qs(params)}`),
  attributionMatch: (body: {
    phones: string[];
    min_duration_seconds?: number;
    date_from?: string;
    date_to?: string;
  }) =>
    request("/api/attribution/match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  // --- Billing (/billing) ---
  // A BulkVS cost ESTIMATE computed from our own CDR — BulkVS publishes no billing API.
  // Charges are per LEG (a forwarded call is billed twice: inbound + outbound), and each
  // row carries the rate it was priced at, so totals never change retroactively.
  billingSummary: (params: { date_from?: string; date_to?: string }) =>
    request(`/api/billing/summary${qs(params)}`),
  billingLegs: (params: {
    date_from?: string;
    date_to?: string;
    direction?: string;
    number_id?: string;
    unrated_only?: boolean;
    limit?: number;
    offset?: number;
  }) => request(`/api/billing/legs${qs(params)}`),
  billingRates: () => request("/api/billing/rates"),
  addBillingAdjustment: (body: {
    occurred_on: string;
    code: string;
    amount: number;
    description?: string;
  }) =>
    request("/api/billing/adjustments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteBillingAdjustment: (id: string) =>
    request(`/api/billing/adjustments/${id}`, { method: "DELETE" }),
  calls: (filters: Record<string, any>) => request(`/api/calls${qs(filters)}`),
  call: (id: string) => request(`/api/calls/${id}`),
  overrideAnalysis: (id: string, body: any) =>
    request(`/api/calls/${id}/analysis`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  numbers: () => request("/api/numbers"),
  // Ticket 15.5: assign/unassign a call flow on a platform number. Contract is exactly
  // {flow_id: string|null}; the backend 400s (detail message) when the flow has no active
  // version or the number is not asterisk-managed.
  updateNumber: (id: string, body: { flow_id: string | null }) =>
    request(`/api/numbers/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  // Archived flows are hidden unless asked for — so every existing caller (including the
  // NumberDetail flow picker) stops offering them without needing to know they exist.
  // DELIBERATELY ARGUMENT-FREE: these are passed straight to react-query as `queryFn`,
  // which calls them with its context object. A `flows(archived = false)` signature would
  // receive that truthy object as `archived` and silently start including archived flows
  // in the number-assignment picker. Two explicit functions make that impossible.
  flows: () => request("/api/flows"),
  flowsIncludingArchived: () => request("/api/flows?archived=true"),
  flow: (id: string) => request(`/api/flows/${id}`),
  createFlow: (name: string) =>
    request("/api/flows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  // Copy a flow's active (else latest) graph into a new DRAFT flow — the "don't start from
  // an empty canvas" path. Nothing is assigned to the copy; agents stay shared.
  cloneFlow: (id: string, name: string) =>
    request(`/api/flows/${id}/clone`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  renameFlow: (id: string, name: string) =>
    request(`/api/flows/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  // Returns {outcome: "deleted" | "archived"} — a flow any call is attributed to can only
  // ever be archived, because calls.flow_version_id IS that call's attribution.
  deleteFlow: (id: string) => request(`/api/flows/${id}`, { method: "DELETE" }),
  restoreFlow: (id: string) => request(`/api/flows/${id}/restore`, { method: "POST" }),
  flowVersions: (flowId: string) => request(`/api/flows/${flowId}/versions`),
  saveFlowVersion: (flowId: string, graph: any) =>
    request(`/api/flows/${flowId}/versions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ graph }),
    }),
  activateFlowVersion: (flowId: string, versionId: string) =>
    request(`/api/flows/${flowId}/versions/${versionId}/activate`, { method: "POST" }),
  agents: () => request("/api/agents"),
  agent: (id: string) => request(`/api/agents/${id}`),
  createAgent: (name: string) =>
    request("/api/agents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  saveAgentVersion: (agentId: string, config: any) =>
    request(`/api/agents/${agentId}/versions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    }),
  activateAgentVersion: (agentId: string, versionId: string) =>
    request(`/api/agents/${agentId}/versions/${versionId}/activate`, { method: "POST" }),
  campaigns: () => request("/api/campaigns"),
  callers: (filters: Record<string, any>) => request(`/api/callers${qs(filters)}`),
  updateCaller: (id: string, body: any) =>
    request(`/api/callers/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  settings: () => request("/api/settings"),
  playUrl: (recordingId: string) => request(`/api/recordings/${recordingId}/play`),
  emails: (filters: Record<string, any>) => request(`/api/emails${qs(filters)}`),
  email: (id: string) => request(`/api/emails/${id}`),
  relayEmail: (id: string) => request(`/api/emails/${id}/relay`, { method: "POST" }),
  messageThreads: (params: Record<string, any> = {}) => request(`/api/messages/threads${qs(params)}`),
  messageThread: (params: { number_id?: string; caller_id?: string }) =>
    request(`/api/messages/thread${qs(params)}`),
  // Manual outbound reply (Ticket 10). Gated server-side on the number's 10DLC status + the
  // contact's opt-out; refused with 409 otherwise.
  sendMessage: (body: { number_id: string; contact: string; body: string }) =>
    request("/api/messages/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  // --- Quo-style per-contact Inbox (/inbox) ---
  // One thread per CONTACT across all BulkVS DIDs; messages + calls in one timeline.
  inboxThreads: () => request("/api/inbox/threads"),
  inboxThread: (callerId: string) => request(`/api/inbox/thread/${callerId}`),
  inboxMarkRead: (callerId: string) =>
    request(`/api/inbox/thread/${callerId}/read`, { method: "POST" }),
  inboxSetClosed: (callerId: string, closed: boolean) =>
    request(`/api/inbox/thread/${callerId}/state`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ closed }),
    }),
  inboxSetBlocked: (callerId: string, blocked: boolean) =>
    request(`/api/inbox/thread/${callerId}/block`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ blocked }),
    }),
  inboxSetDeleted: (callerId: string, deleted: boolean) =>
    request(`/api/inbox/thread/${callerId}/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deleted }),
    }),
  inboxUpdateContact: (callerId: string, body: { name?: string | null; company?: string | null; role?: string | null }) =>
    request(`/api/inbox/contacts/${callerId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  inboxAddNote: (callerId: string, body: string) =>
    request(`/api/inbox/contacts/${callerId}/notes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body }),
    }),
  inboxDeleteNote: (noteId: string) =>
    request(`/api/inbox/notes/${noteId}`, { method: "DELETE" }),
  inboxSettings: () => request("/api/inbox/settings"),
  inboxSetDefaultNumber: (number_id: string | null) =>
    request("/api/inbox/settings/default-number", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ number_id }),
    }),
  // Send resolving the from-DID server-side (override > sticky > default; 10DLC-gated).
  inboxSend: (body: { contact: string; body: string; number_id?: string }) =>
    request("/api/inbox/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  // --- Operator WebRTC softphone (Ticket 13) ---
  // Mint short-lived SIP + TURN creds for this operator's browser softphone (app-login gate).
  webrtcCredentials: () => request("/api/telephony/webrtc/credentials", { method: "POST" }),
  // Enrich a ringing call for the incoming-call popup (Ticket 18): caller -> contact label,
  // dialed DID -> friendly name. Unknown numbers come back null and the UI shows raw digits.
  incomingContext: (caller: string, dialed: string) =>
    request(
      `/api/telephony/incoming-context?caller=${encodeURIComponent(caller)}&dialed=${encodeURIComponent(dialed)}`,
    ),
  // Backend-driven control ops (SIP.js NEVER touches ARI): hold / bridge / blind-transfer.
  telephonyHold: (channel_id: string, hold: boolean) =>
    request("/api/telephony/control/hold", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel_id, hold }),
    }),
  telephonyBridge: (channel_a: string, channel_b: string) =>
    request("/api/telephony/control/bridge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel_a, channel_b }),
    }),
  telephonyTransfer: (channel_id: string, kind: string, target: string) =>
    request("/api/telephony/control/transfer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel_id, kind, target }),
    }),
  // Manual operator outbound calling (Ticket 14). Owned BulkVS DIDs for the from-number picker,
  // and the "place outbound call" orchestration (originate + pre-bridge consent + bridge, ARI).
  outboundFromNumbers: () => request("/api/telephony/outbound/from-numbers"),
  // The backend rings THIS operator's softphone first and then bridges it to the callee, so
  // our own outgoing call arrives as an ordinary incoming INVITE. Mark the intent here — the
  // one place every caller goes through — so the softphone auto-answers that leg into the
  // in-call UI instead of popping "Incoming call" for the number we just dialed.
  outboundCall: async (callee_number: string, from_number: string) => {
    markOutboundIntent();
    try {
      const res = await request("/api/telephony/outbound/call", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ callee_number, from_number }),
      });
      // The backend originate returns the callee's channel id — the exact channel the in-call
      // panel holds / blind-transfers over ARI (no correlation needed for outbound). Stamp it,
      // plus the from-number as this call's "line", for the panel to read.
      setActiveCall({
        channelId: (res as any)?.callee_channel ?? null,
        line: from_number,
        direction: "outbound",
      });
      return res;
    } catch (e) {
      clearOutboundIntent(); // the call was never placed; no INVITE is coming
      throw e;
    }
  },
};

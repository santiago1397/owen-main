export const API_BASE = (import.meta as any).env?.VITE_API_BASE || "";

const TOKEN_KEY = "callmon_token";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}
export function setToken(t: string) {
  localStorage.setItem(TOKEN_KEY, t);
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request(path: string, opts: RequestInit = {}): Promise<any> {
  const headers: Record<string, string> = { ...(opts.headers as any) };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${API_BASE}${path}`, { ...opts, headers });
  if (res.status === 401) {
    clearToken();
    if (!location.pathname.startsWith("/login")) location.href = "/login";
    throw new ApiError(401, "unauthorized");
  }
  if (!res.ok) throw new ApiError(res.status, await res.text());
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
  setToken(data.access_token);
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
  calls: (filters: Record<string, any>) => request(`/api/calls${qs(filters)}`),
  call: (id: string) => request(`/api/calls/${id}`),
  overrideAnalysis: (id: string, body: any) =>
    request(`/api/calls/${id}/analysis`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  numbers: () => request("/api/numbers"),
  flows: () => request("/api/flows"),
  flow: (id: string) => request(`/api/flows/${id}`),
  createFlow: (name: string) =>
    request("/api/flows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  saveFlowVersion: (flowId: string, graph: any) =>
    request(`/api/flows/${flowId}/versions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ graph }),
    }),
  activateFlowVersion: (flowId: string, versionId: string) =>
    request(`/api/flows/${flowId}/versions/${versionId}/activate`, { method: "POST" }),
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
};

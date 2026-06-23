const TOKEN_KEY = "fw_token";
const ROLE_KEY = "fw_role";
const USER_KEY = "fw_user";

export function getToken() { return localStorage.getItem(TOKEN_KEY); }
export function getRole() { return localStorage.getItem(ROLE_KEY) || ""; }
export function getUser() { return localStorage.getItem(USER_KEY) || ""; }
export function setAuth(t: string, role: string, user: string) {
  localStorage.setItem(TOKEN_KEY, t);
  localStorage.setItem(ROLE_KEY, role);
  localStorage.setItem(USER_KEY, user);
}
export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(ROLE_KEY);
  localStorage.removeItem(USER_KEY);
}

async function req(path: string, opts: RequestInit = {}) {
  const headers: Record<string, string> = { ...(opts.headers as any) };
  const t = getToken();
  if (t) headers["Authorization"] = `Bearer ${t}`;
  if (opts.body && !(opts.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) { clearAuth(); window.location.href = "/login"; throw new Error("401"); }
  if (!res.ok) {
    let msg = `Ошибка ${res.status}`;
    try { const j = await res.json(); msg = j.detail || msg; } catch {}
    throw new Error(msg);
  }
  const ct = res.headers.get("Content-Type") || "";
  return ct.includes("application/json") ? res.json() : res;
}

export const api = {
  login: async (username: string, password: string) => {
    const fd = new FormData();
    fd.append("username", username);
    fd.append("password", password);
    const res = await fetch("/api/auth/login", { method: "POST", body: fd });
    if (!res.ok) { const j = await res.json().catch(() => ({})); throw new Error(j.detail || "Ошибка входа"); }
    return res.json();
  },
  me: () => req("/api/auth/me"),
  changePassword: (old_password: string, new_password: string) =>
    req("/api/auth/change-password", { method: "POST", body: JSON.stringify({ old_password, new_password }) }),
  testRtsp: (rtsp_url: string) =>
    req("/api/cameras/test", { method: "POST", body: JSON.stringify({ rtsp_url }) }),
  cameras: () => req("/api/cameras"),
  camAdd: (b: any) => req("/api/cameras", { method: "POST", body: JSON.stringify(b) }),
  camUpdate: (id: number, b: any) => req(`/api/cameras/${id}`, { method: "PUT", body: JSON.stringify(b) }),
  camDelete: (id: number) => req(`/api/cameras/${id}`, { method: "DELETE" }),
  camToggle: (id: number, enabled: boolean) =>
    req(`/api/cameras/${id}/enabled?enabled=${enabled}`, { method: "PATCH" }),
  camRoiGet: (id: number) => req(`/api/cameras/${id}/roi`),
  camRoiPut: (id: number, polygons: number[][][]) =>
    req(`/api/cameras/${id}/roi`, { method: "PUT", body: JSON.stringify({ polygons }) }),
  camHls: (id: number) => req(`/api/cameras/${id}/hls`),
  persons: (params: { status?: string; q?: string; page?: number; page_size?: number } = {}) => {
    const usp = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== "") usp.set(k, String(v)); });
    return req(`/api/persons?${usp.toString()}`);
  },
  personGet: (id: number) => req(`/api/persons/${id}`),
  personUpdate: (id: number, b: any) => req(`/api/persons/${id}`, { method: "PATCH", body: JSON.stringify(b) }),
  personMerge: (src: number, dst: number) => req(`/api/persons/${src}/merge/${dst}`, { method: "POST" }),
  personGallery: (id: number) => req(`/api/persons/${id}/gallery`),
  personEnhance: (id: number) => req(`/api/persons/${id}/enhance`, { method: "POST" }),
  searchFace: (form: FormData) => req("/api/search/face", { method: "POST", body: form }),
  events: (limit = 100) => req(`/api/events?limit=${limit}`),
  archive: (params: Record<string, string>) => {
    const q = new URLSearchParams(params).toString();
    return req(`/api/archive/segments?${q}`);
  },
  kpi: () => req("/api/stats/kpi"),
  byDay: () => req("/api/stats/by-day"),
  byHour: () => req("/api/stats/by-hour"),
  heatmap: () => req("/api/stats/heatmap"),
  topPersons: () => req("/api/stats/top-persons"),
  users: () => req("/api/users"),
  userAdd: (b: any) => req("/api/users", { method: "POST", body: JSON.stringify(b) }),
  userDel: (id: number) => req(`/api/users/${id}`, { method: "DELETE" }),
  getSettings: () => req("/api/settings"),
  putSettings: (b: any) => req("/api/settings", { method: "PUT", body: JSON.stringify(b) }),
  health: async () => {
    const r = await fetch("/api/health");
    try { return await r.json(); } catch { return { ok: false, db: "?", redis: "?" }; }
  },
  raw: (path: string) => req(path),
};

export function mediaUrl(rel: string | null | undefined): string {
  if (!rel) return "";
  const [kind, name] = rel.split("/");
  const t = getToken();
  return `/api/media/${kind}/${name}?token=${t}`;
}

export function camSnapshotUrl(id: number): string {
  return `/api/cameras/${id}/snapshot?token=${getToken()}&t=${Date.now()}`;
}

export function wsUrl(path: string): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}${path}?token=${getToken()}`;
}

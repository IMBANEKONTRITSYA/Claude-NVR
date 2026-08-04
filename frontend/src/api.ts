const TOKEN_KEY = "fw_token";
const REFRESH_KEY = "fw_refresh";
const ROLE_KEY = "fw_role";
const USER_KEY = "fw_user";
const PWD_EXPIRED_KEY = "fw_pwd_expired";

export function getToken() { return localStorage.getItem(TOKEN_KEY); }
export function getRefreshToken() { return localStorage.getItem(REFRESH_KEY); }
export function getRole() { return localStorage.getItem(ROLE_KEY) || ""; }
export function getUser() { return localStorage.getItem(USER_KEY) || ""; }
// ТЗ 13: "срок действия пароля" — бэкенд помечает флагом ответ login/refresh,
// когда пароль просрочен (settings.PASSWORD_MAX_AGE_DAYS); фронтенд
// принудительно ведёт на смену пароля, не блокируя сам вход.
export function isPasswordExpired() { return localStorage.getItem(PWD_EXPIRED_KEY) === "1"; }
export function setAuth(t: string, refresh: string, role: string, user: string, passwordExpired = false) {
  localStorage.setItem(TOKEN_KEY, t);
  localStorage.setItem(REFRESH_KEY, refresh);
  localStorage.setItem(ROLE_KEY, role);
  localStorage.setItem(USER_KEY, user);
  if (passwordExpired) localStorage.setItem(PWD_EXPIRED_KEY, "1");
  else localStorage.removeItem(PWD_EXPIRED_KEY);
}
export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REFRESH_KEY);
  localStorage.removeItem(ROLE_KEY);
  localStorage.removeItem(USER_KEY);
  localStorage.removeItem(PWD_EXPIRED_KEY);
}

// Access-токен живёт недолго (см. ACCESS_TOKEN_EXPIRE_MINUTES) — вместо
// разлогинивания на каждый 401 пробуем один раз обновить его через
// refresh-токен и повторить запрос. refreshPromise дедуплицирует
// параллельные 401 (например, несколько виджетов дашборда одновременно).
let refreshPromise: Promise<string | null> | null = null;

async function doRefresh(): Promise<string | null> {
  const rt = getRefreshToken();
  if (!rt) return null;
  try {
    const res = await fetch("/api/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: rt }),
    });
    if (!res.ok) return null;
    const j = await res.json();
    setAuth(j.access_token, j.refresh_token, j.role, j.username, j.password_expired);
    return j.access_token as string;
  } catch {
    return null;
  }
}

// FastAPI отдаёт detail строкой для наших HTTPException, но массивом
// объектов {msg, loc, ...} для ошибок валидации Pydantic (422) — например,
// пароль не прошедший политику сложности. Без этой распаковки пользователь
// увидел бы "[object Object]" вместо текста ошибки.
function errorMessage(j: any, fallback: string): string {
  if (typeof j?.detail === "string") return j.detail;
  if (Array.isArray(j?.detail)) return j.detail.map((d: any) => d?.msg || String(d)).join("; ");
  return fallback;
}

async function req(path: string, opts: RequestInit = {}, retried = false): Promise<any> {
  const headers: Record<string, string> = { ...(opts.headers as any) };
  const t = getToken();
  if (t) headers["Authorization"] = `Bearer ${t}`;
  if (opts.body && !(opts.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    if (!retried && getRefreshToken()) {
      refreshPromise = refreshPromise || doRefresh();
      const newToken = await refreshPromise;
      refreshPromise = null;
      if (newToken) return req(path, opts, true);
    }
    clearAuth();
    window.location.href = "/login";
    throw new Error("401");
  }
  if (!res.ok) {
    let msg = `Ошибка ${res.status}`;
    try { const j = await res.json(); msg = errorMessage(j, msg); } catch {}
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
    if (!res.ok) { const j = await res.json().catch(() => ({})); throw new Error(errorMessage(j, "Ошибка входа")); }
    return res.json();
  },
  me: () => req("/api/auth/me"),
  logout: async () => {
    const rt = getRefreshToken();
    if (rt) {
      // Best-effort: даже если запрос не дойдёт (сеть/сервер недоступен),
      // локальный выход всё равно должен сработать.
      try {
        await fetch("/api/auth/logout", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: rt }),
        });
      } catch { /* локальный logout ниже отработает в любом случае */ }
    }
    clearAuth();
  },
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
  personCreate: (form: FormData) => req("/api/persons", { method: "POST", body: form }),
  personGet: (id: number) => req(`/api/persons/${id}`),
  personUpdate: (id: number, b: any) => req(`/api/persons/${id}`, { method: "PATCH", body: JSON.stringify(b) }),
  personMerge: (src: number, dst: number) => req(`/api/persons/${src}/merge/${dst}`, { method: "POST" }),
  personGallery: (id: number) => req(`/api/persons/${id}/gallery`),
  personEnhance: (id: number) => req(`/api/persons/${id}/enhance`, { method: "POST" }),
  personDelete: (id: number) => req(`/api/persons/${id}`, { method: "DELETE" }),
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
  testTelegram: () => req("/api/settings/test-telegram", { method: "POST" }),
  listProfiles: () => req("/api/settings/profiles"),
  applyProfile: (name: string) => req(`/api/settings/profile/${name}`, { method: "POST" }),
  sysMetrics: () => req("/api/system/metrics"),
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

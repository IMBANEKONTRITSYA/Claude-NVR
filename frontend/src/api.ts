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
// /hls/ (nginx auth_request, см. nginx-locations.conf) не может нести
// Authorization-заголовок или ?token= — HLS-плеер сам дергает .ts-сегменты
// по относительным URI из плейлиста, куда query string исходного запроса
// не переносится. Кука с тем же access-токеном, ограниченная path=/hls/, —
// браузер прикрепляет её к каждому такому запросу автоматически (P0, цикл 6).
function setHlsAuthCookie(t: string) {
  const secure = location.protocol === "https:" ? "; Secure" : "";
  document.cookie = `hls_auth=${t}; path=/hls/; SameSite=Strict${secure}`;
}
function clearHlsAuthCookie() {
  document.cookie = "hls_auth=; path=/hls/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
}
export function setAuth(t: string, refresh: string, role: string, user: string, passwordExpired = false) {
  localStorage.setItem(TOKEN_KEY, t);
  localStorage.setItem(REFRESH_KEY, refresh);
  localStorage.setItem(ROLE_KEY, role);
  localStorage.setItem(USER_KEY, user);
  if (passwordExpired) localStorage.setItem(PWD_EXPIRED_KEY, "1");
  else localStorage.removeItem(PWD_EXPIRED_KEY);
  setHlsAuthCookie(t);
}
export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REFRESH_KEY);
  localStorage.removeItem(ROLE_KEY);
  localStorage.removeItem(USER_KEY);
  localStorage.removeItem(PWD_EXPIRED_KEY);
  clearHlsAuthCookie();
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
  onvifDiscover: (subnet?: string) =>
    req(`/api/cameras/onvif/discover${subnet ? `?subnet=${encodeURIComponent(subnet)}` : ""}`),
  onvifBulkAdd: (cameras: any[], enabled = true, onvif_enabled = true) =>
    req("/api/cameras/onvif/bulk-add", {
      method: "POST",
      body: JSON.stringify({ cameras, enabled, onvif_enabled }),
    }),
  onvifProfiles: (host: string, port: number, username: string, password: string) =>
    req("/api/cameras/onvif/profiles", { method: "POST", body: JSON.stringify({ host, port, username, password }) }),
  onvifStreamUri: (host: string, port: number, username: string, password: string, profile_token: string) =>
    req("/api/cameras/onvif/stream-uri", {
      method: "POST",
      body: JSON.stringify({ host, port, username, password, profile_token }),
    }),
  cameras: () => req("/api/cameras"),
  // SPEC §3: импорт/экспорт конфигурации камер. Выгрузка открывается
  // прямой ссылкой (см. camerasExportUrl), загрузка идёт multipart'ом.
  camerasImport: (file: File, dry_run = false) => {
    const fd = new FormData();
    fd.append("file", file);
    return req(`/api/cameras/import?dry_run=${dry_run ? "1" : "0"}`, { method: "POST", body: fd });
  },
  camAdd: (b: any) => req("/api/cameras", { method: "POST", body: JSON.stringify(b) }),
  camUpdate: (id: number, b: any) => req(`/api/cameras/${id}`, { method: "PUT", body: JSON.stringify(b) }),
  camDelete: (id: number) => req(`/api/cameras/${id}`, { method: "DELETE" }),
  // Основной RTSP-адрес в открытом виде: в БД он зашифрован, и CameraOut
  // его не отдаёт. Нужен форме редактирования — без него «Изм.» открывала
  // форму с пустым полем адреса, и сохранение падало валидацией.
  camRtsp: (id: number) => req(`/api/cameras/${id}/rtsp`),
  camToggle: (id: number, enabled: boolean) =>
    req(`/api/cameras/${id}/enabled?enabled=${enabled}`, { method: "PATCH" }),
  camRoiGet: (id: number) => req(`/api/cameras/${id}/roi`),
  camRoiPut: (id: number, polygons: number[][][]) =>
    req(`/api/cameras/${id}/roi`, { method: "PUT", body: JSON.stringify({ polygons }) }),
  camHls: (id: number) => req(`/api/cameras/${id}/hls`),
  // SPEC §4: PTZ. profile_token берётся из camPtz() и передаётся с каждой
  // командой — иначе воркер резолвит профиль камеры заново на каждое
  // нажатие стрелки (лишний GetProfiles к камере в интерактивном пути).
  camPtz: (id: number) => req(`/api/cameras/${id}/ptz`),
  camPtzMove: (id: number, v: { pan?: number; tilt?: number; zoom?: number; profile_token?: string | null }) =>
    req(`/api/cameras/${id}/ptz/move`, { method: "POST", body: JSON.stringify(v) }),
  camPtzStop: (id: number, profile_token?: string | null) =>
    req(`/api/cameras/${id}/ptz/stop`, { method: "POST", body: JSON.stringify({ profile_token }) }),
  camPtzGoto: (id: number, preset_token: string, profile_token?: string | null) =>
    req(`/api/cameras/${id}/ptz/preset/goto`, {
      method: "POST", body: JSON.stringify({ preset_token, profile_token }),
    }),
  camPtzSavePreset: (id: number, name: string, profile_token?: string | null) =>
    req(`/api/cameras/${id}/ptz/preset/save`, {
      method: "POST", body: JSON.stringify({ name, profile_token }),
    }),
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
  /** Шкала архива камеры за окно (SPEC §5): покрытие, дыры, цепочка файлов. */
  archiveTimeline: (cameraId: number, dateFrom: string, dateTo: string) => {
    const q = new URLSearchParams({
      camera_id: String(cameraId), date_from: dateFrom, date_to: dateTo,
    }).toString();
    return req(`/api/archive/timeline?${q}`);
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
  testEmail: () => req("/api/settings/test-email", { method: "POST" }),

  // Шаблоны отчётов и расписание (SPEC §8)
  reportKinds: () => req("/api/reports/kinds"),
  reportSchedules: () => req("/api/reports/schedules"),
  createReportSchedule: (body: any) =>
    req("/api/reports/schedules", { method: "POST", body: JSON.stringify(body) }),
  updateReportSchedule: (id: number, body: any) =>
    req(`/api/reports/schedules/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteReportSchedule: (id: number) =>
    req(`/api/reports/schedules/${id}`, { method: "DELETE" }),
  sendReportSchedule: (id: number) =>
    req(`/api/reports/schedules/${id}/send`, { method: "POST" }),
  // Несекретные настройки интерфейса — доступны под любой ролью, в отличие
  // от getSettings() (admin-only, отдаёт расшифрованные секреты).
  getClientSettings: () => req("/api/settings/client"),
  listProfiles: () => req("/api/settings/profiles"),
  applyProfile: (name: string) => req(`/api/settings/profile/${name}`, { method: "POST" }),
  sysMetrics: () => req("/api/system/metrics"),
  // SPEC §5, §21: заполнение диска, фактический расход, прогноз хранения
  storage: () => req("/api/system/storage"),
  // SPEC §14, §9: статус потоков слоя записи (число камер — сколько заведено)
  recordLayer: () => req("/api/system/record-layer"),
  // SPEC §16. `cameras`/`days` необязательны: без них сервер подставляет
  // фактическое число включённых камер и настроенный retention, и первый
  // ответ относится к этой системе, а не к вымышленной. §22 прямо
  // запрещает хардкодить количество камер.
  storageCalc: (bitrate_kbps: number, cameras?: number, days?: number) => {
    const q = new URLSearchParams({ bitrate_kbps: String(bitrate_kbps) });
    if (cameras !== undefined) q.set("cameras", String(cameras));
    if (days !== undefined) q.set("days", String(days));
    return req(`/api/system/storage/calculator?${q}`);
  },
  // SPEC §16 «Автоконфигурация при первом запуске»: ресурсы сервера →
  // предложение по числу камер записи, камер аналитики и профилю.
  autoconfig: (bitrate_kbps: number, retention_days: number) =>
    req(`/api/system/autoconfig?bitrate_kbps=${bitrate_kbps}&retention_days=${retention_days}`),
  autoconfigApply: (bitrate_kbps: number, retention_days: number) =>
    req(`/api/system/autoconfig/apply?bitrate_kbps=${bitrate_kbps}&retention_days=${retention_days}`,
        { method: "POST" }),
  // SPEC §11 «Резервное копирование: автоматическое раз в сутки + ручной
  // запуск», §18 строка «Управление бэкапами» — все четыре admin-only.
  listBackups: () => req("/api/system/backups"),
  createBackup: () => req("/api/system/backups", { method: "POST" }),
  deleteBackup: (name: string) =>
    req(`/api/system/backups/${encodeURIComponent(name)}`, { method: "DELETE" }),
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

// SPEC §3: ссылка на выгрузку конфигурации камер. Как и отчёты, файл
// скачивается прямой ссылкой браузера, поэтому токен идёт в query string.
export function camerasExportUrl(format: "csv" | "json", includeSecrets: boolean): string {
  return `/api/cameras/export?format=${format}&include_secrets=${includeSecrets ? "1" : "0"}&token=${getToken()}`;
}

// SPEC §7 «миниатюры кадров для быстрого просмотра». Адрес идёт в
// <img src>, заголовок к нему не прикрепить — токен в query string, как у
// скачивания сегмента и экспорта.
//
// Без метки времени в адресе, в отличие от camSnapshotUrl: снимок камеры
// живой и обязан перезапрашиваться, а кадр закрытого сегмента иммутабелен.
// Метка сбивала бы кэш браузера, и выдача из 200 строк тянула бы двести
// картинок заново при каждом уточнении фильтра.
export function segmentThumbUrl(id: number): string {
  return `/api/archive/thumb/${id}?token=${getToken()}`;
}

// SPEC §11. Дамп забирает сам браузер прямой ссылкой (файл на сотни
// мегабайт незачем тянуть через fetch в память вкладки), поэтому токен —
// в query string, как у отчётов и выгрузки конфигурации камер.
export function backupDownloadUrl(name: string): string {
  return `/api/system/backups/${encodeURIComponent(name)}?token=${getToken()}`;
}

export function camSnapshotUrl(id: number): string {
  return `/api/cameras/${id}/snapshot?token=${getToken()}&t=${Date.now()}`;
}

export function wsUrl(path: string): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}${path}?token=${getToken()}`;
}

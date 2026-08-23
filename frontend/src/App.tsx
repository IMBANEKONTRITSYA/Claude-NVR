import { NavLink, Route, Routes, Navigate, useLocation, useNavigate } from "react-router-dom";
import { Suspense, lazy, useCallback, useEffect, useState } from "react";
import { api, getRole, getToken, getUser, isPasswordExpired } from "./api";
import { Login } from "./pages/Login";
import { useInactivityLogout } from "./useInactivityLogout";
import { canAccess, SECTION_ROLES, type Section } from "./access";

const Dashboard = lazy(() => import("./pages/Dashboard").then(m => ({ default: m.Dashboard })));
const LiveGrid = lazy(() => import("./pages/LiveGrid").then(m => ({ default: m.LiveGrid })));
const Wall = lazy(() => import("./pages/Wall").then(m => ({ default: m.Wall })));
const Cameras = lazy(() => import("./pages/Cameras").then(m => ({ default: m.Cameras })));
const Persons = lazy(() => import("./pages/Persons").then(m => ({ default: m.Persons })));
const Archive = lazy(() => import("./pages/Archive").then(m => ({ default: m.Archive })));
const ROI = lazy(() => import("./pages/ROI").then(m => ({ default: m.ROI })));
const Users = lazy(() => import("./pages/Users").then(m => ({ default: m.Users })));
const Reports = lazy(() => import("./pages/Reports").then(m => ({ default: m.Reports })));
const Search = lazy(() => import("./pages/Search").then(m => ({ default: m.Search })));
const SettingsPage = lazy(() => import("./pages/Settings").then(m => ({ default: m.Settings })));
const Monitoring = lazy(() => import("./pages/Monitoring").then(m => ({ default: m.Monitoring })));
const Audit = lazy(() => import("./pages/Audit").then(m => ({ default: m.Audit })));
const Profile = lazy(() => import("./pages/Profile").then(m => ({ default: m.Profile })));

function HealthBadge() {
  const [h, setH] = useState<any>({ ok: true, db: "?", redis: "?" });
  useEffect(() => {
    const tick = () => api.health().then(setH).catch(() => setH({ ok: false, db: "down", redis: "down" }));
    tick();
    const t = setInterval(tick, 15000);
    return () => clearInterval(t);
  }, []);
  const dot = h.ok ? "ok" : "err";
  const text = h.ok ? "Система в норме" : `Проблема: ${h.db !== "ok" ? "БД" : ""} ${h.redis !== "ok" ? "Redis" : ""}`.trim();
  return <div className="muted" style={{ fontSize: 11, marginBottom: 8 }}><span className={`dot ${dot}`} />{text}</div>;
}

/** Раздел меню. Не рисуется, если роль не даёт ни одного пункта внутри. */
function NavGroup({ title, children }: { title: string; children: any }) {
  const visible = (Array.isArray(children) ? children : [children]).filter(Boolean);
  if (!visible.length) return null;
  return (
    <div className="nav-group">
      <div className="nav-group-title">{title}</div>
      {visible}
    </div>
  );
}

function Layout({ children }: { children: any }) {
  const nav = useNavigate();
  const role = getRole();
  const user = getUser();
  // Матрица прав §18 — из access.ts, а не списками ролей по месту: три
  // независимые копии (роут, пункт меню, require_role) и разошлись на Стене.
  const can = (section: Section) => canAccess(section, role);
  const logout = () => { api.logout().then(() => nav("/login")); };
  const inactivityLogout = useCallback(() => {
    api.logout().finally(() => nav("/login?reason=inactive"));
  }, [nav]);
  useInactivityLogout(true, inactivityLogout);
  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>FaceWatch</h1>
        {/* Пункты сгруппированы по задачам, а не свалены одним списком из
            тринадцати ссылок: раздел ищут взглядом за один проход, а не
            перебором всего меню. Пустые группы (роль не даёт ни одного
            пункта) не рисуются — заголовок без содержимого хуже, чем его
            отсутствие. */}
        <div className="nav-scroll">
          <nav className="nav">
            <NavGroup title="Наблюдение">
              <NavLink to="/dashboard">Дашборд</NavLink>
              <NavLink to="/live">Камеры онлайн</NavLink>
              {/* Стена — раздел модуля распознавания §15, а не наблюдения:
                  она показывает имя, хранимый кадр и watchlist-разметку,
                  то есть карточку персоны. §18 закрывает наблюдателю
                  строку «Карточки персон», поэтому здесь та же роль, что
                  у пунктов группы «Распознавание» ниже. Живой просмотр с
                  рамками §4 наблюдателю остаётся — это «Камеры онлайн». */}
              {can("wall") && <NavLink to="/wall">Стена распознавания</NavLink>}
            </NavGroup>
            <NavGroup title="Распознавание">
              {can("persons") && <NavLink to="/persons">Карточки персон</NavLink>}
              {can("search") && <NavLink to="/search">Поиск по фото</NavLink>}
              {can("roi") && <NavLink to="/roi">Зоны детекции</NavLink>}
            </NavGroup>
            <NavGroup title="Архив и отчёты">
              {can("archive") && <NavLink to="/archive">Видеоархив</NavLink>}
              {can("reports") && <NavLink to="/reports">Отчёты</NavLink>}
            </NavGroup>
            <NavGroup title="Администрирование">
              {can("cameras") && <NavLink to="/cameras">Камеры</NavLink>}
              {can("monitoring") && <NavLink to="/monitoring">Мониторинг</NavLink>}
              {can("settings") && <NavLink to="/settings">Настройки</NavLink>}
              {can("users") && <NavLink to="/users">Пользователи</NavLink>}
              {can("audit") && <NavLink to="/audit">Журнал действий</NavLink>}
            </NavGroup>
          </nav>
        </div>
        <div style={{ marginTop: 16, paddingTop: 16, borderTop: "1px solid var(--border)" }}>
          <HealthBadge />
          <NavLink to="/profile" style={{ display: "block", fontSize: 12, marginBottom: 8 }}>
            {user} · {role}
          </NavLink>
          <button className="btn secondary" style={{ width: "100%" }} onClick={logout}>Выйти</button>
        </div>
      </aside>
      <main className="main">{children}</main>
    </div>
  );
}

function Private({ children, section }: { children: any; section?: Section }) {
  const location = useLocation();
  if (!getToken()) return <Navigate to="/login" replace />;
  // ТЗ 13: срок действия пароля истёк — пускаем только на страницу его смены,
  // не блокируя вход полностью.
  if (isPasswordExpired() && location.pathname !== "/profile") {
    return <Navigate to="/profile" replace />;
  }
  // Роли раздела берутся из той же таблицы §18, что и пункты меню
  // (access.ts). Раньше они дублировались списком по месту, и роут /wall
  // разошёлся с матрицей: он был открыт любой роли.
  if (section && !canAccess(section, getRole())) return <Navigate to="/dashboard" replace />;
  return <Layout>{children}</Layout>;
}

export function App() {
  return (
    <Suspense fallback={<div className="empty">Загрузка...</div>}>
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/dashboard" element={<Private><Dashboard /></Private>} />
      <Route path="/live" element={<Private><LiveGrid /></Private>} />
      <Route path="/wall" element={<Private section="wall"><Wall /></Private>} />
      <Route path="/persons" element={<Private section="persons"><Persons /></Private>} />
      <Route path="/archive" element={<Private section="archive"><Archive /></Private>} />
      <Route path="/roi" element={<Private section="roi"><ROI /></Private>} />
      <Route path="/reports" element={<Private section="reports"><Reports /></Private>} />
      <Route path="/search" element={<Private section="search"><Search /></Private>} />
      <Route path="/cameras" element={<Private section="cameras"><Cameras /></Private>} />
      <Route path="/users" element={<Private section="users"><Users /></Private>} />
      <Route path="/monitoring" element={<Private section="monitoring"><Monitoring /></Private>} />
      <Route path="/settings" element={<Private section="settings"><SettingsPage /></Private>} />
      <Route path="/audit" element={<Private section="audit"><Audit /></Private>} />
      <Route path="/profile" element={<Private><Profile /></Private>} />
      <Route path="*" element={<Navigate to={getToken() ? "/dashboard" : "/login"} replace />} />
    </Routes>
    </Suspense>
  );
}

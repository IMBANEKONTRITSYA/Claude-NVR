import { NavLink, Route, Routes, Navigate, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { clearAuth, getRole, getToken, getUser } from "./api";
import { Login } from "./pages/Login";
import { Dashboard } from "./pages/Dashboard";
import { LiveGrid } from "./pages/LiveGrid";
import { Wall } from "./pages/Wall";
import { Cameras } from "./pages/Cameras";
import { Persons } from "./pages/Persons";
import { Archive } from "./pages/Archive";
import { ROI } from "./pages/ROI";
import { Users } from "./pages/Users";
import { Reports } from "./pages/Reports";

function Layout({ children }: { children: any }) {
  const nav = useNavigate();
  const role = getRole();
  const user = getUser();
  const can = (...roles: string[]) => roles.includes(role);
  const logout = () => { clearAuth(); nav("/login"); };
  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>FaceWatch</h1>
        <nav className="nav">
          <NavLink to="/dashboard">Дашборд</NavLink>
          <NavLink to="/live">Камеры онлайн</NavLink>
          <NavLink to="/wall">Стена распознавания</NavLink>
          {can("admin", "operator") && <NavLink to="/persons">Карточки персон</NavLink>}
          {can("admin", "operator") && <NavLink to="/archive">Архив</NavLink>}
          {can("admin", "operator") && <NavLink to="/roi">Зоны детекции</NavLink>}
          {can("admin", "operator") && <NavLink to="/reports">Отчёты</NavLink>}
          {can("admin") && <NavLink to="/cameras">Управление камерами</NavLink>}
          {can("admin") && <NavLink to="/users">Пользователи</NavLink>}
        </nav>
        <div style={{ marginTop: 24, paddingTop: 16, borderTop: "1px solid var(--border)" }}>
          <div className="muted" style={{ fontSize: 12 }}>{user} · {role}</div>
          <button className="btn secondary" style={{ marginTop: 8, width: "100%" }} onClick={logout}>Выйти</button>
        </div>
      </aside>
      <main className="main">{children}</main>
    </div>
  );
}

function Private({ children, roles }: { children: any; roles?: string[] }) {
  if (!getToken()) return <Navigate to="/login" replace />;
  if (roles && !roles.includes(getRole())) return <Navigate to="/dashboard" replace />;
  return <Layout>{children}</Layout>;
}

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/dashboard" element={<Private><Dashboard /></Private>} />
      <Route path="/live" element={<Private><LiveGrid /></Private>} />
      <Route path="/wall" element={<Private><Wall /></Private>} />
      <Route path="/persons" element={<Private roles={["admin", "operator"]}><Persons /></Private>} />
      <Route path="/archive" element={<Private roles={["admin", "operator"]}><Archive /></Private>} />
      <Route path="/roi" element={<Private roles={["admin", "operator"]}><ROI /></Private>} />
      <Route path="/reports" element={<Private roles={["admin", "operator"]}><Reports /></Private>} />
      <Route path="/cameras" element={<Private roles={["admin"]}><Cameras /></Private>} />
      <Route path="/users" element={<Private roles={["admin"]}><Users /></Private>} />
      <Route path="*" element={<Navigate to={getToken() ? "/dashboard" : "/login"} replace />} />
    </Routes>
  );
}

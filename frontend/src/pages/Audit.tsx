import { useEffect, useState } from "react";
import { api } from "../api";

export function Audit() {
  const [rows, setRows] = useState<any[]>([]);
  const [user, setUser] = useState("");

  const load = () => {
    const url = user ? `/api/audit?username=${encodeURIComponent(user)}` : "/api/audit";
    api.raw(url).then(setRows).catch(() => {});
  };
  useEffect(() => { load(); }, [user]);

  const statusColor = (s: number) =>
    s < 300 ? "var(--green)" : s < 400 ? "var(--accent)" : s < 500 ? "var(--orange)" : "var(--red)";

  return (
    <div>
      <h2>Журнал действий</h2>
      <div className="toolbar">
        <input placeholder="Фильтр по пользователю" value={user} onChange={e => setUser(e.target.value)} style={{ width: 240 }} />
        <button className="btn secondary" onClick={load}>Обновить</button>
      </div>
      <div className="card">
        <table>
          <thead><tr><th>Время</th><th>Пользователь</th><th>Действие</th><th>Метод</th><th>Путь</th><th>Код</th><th>IP</th></tr></thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.id}>
                <td>{new Date(r.ts).toLocaleString("ru-RU")}</td>
                <td>{r.username} <span className="muted">({r.role})</span></td>
                <td>{r.action}</td>
                <td>{r.method}</td>
                <td className="muted" style={{ fontSize: 12 }}>{r.path}</td>
                <td style={{ color: statusColor(r.status_code), fontWeight: 600 }}>{r.status_code}</td>
                <td className="muted">{r.ip || "—"}</td>
              </tr>
            ))}
            {rows.length === 0 && <tr><td colSpan={7} className="empty">Записей нет</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

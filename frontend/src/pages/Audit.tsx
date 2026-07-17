import { useEffect, useState } from "react";
import { api, getToken } from "../api";
import { Pager } from "../Pager";

export function Audit() {
  const [rows, setRows] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const PAGE_SIZE = 50;
  const [user, setUser] = useState("");
  const [action, setAction] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");

  const load = () => {
    const usp = new URLSearchParams();
    if (user) usp.set("username", user);
    if (action) usp.set("action", action);
    if (from) usp.set("date_from", from);
    if (to) usp.set("date_to", to);
    usp.set("page", String(page));
    usp.set("page_size", String(PAGE_SIZE));
    api.raw(`/api/audit?${usp.toString()}`).then((r: any) => { setRows(r.items); setTotal(r.total); }).catch(() => {});
  };

  useEffect(() => { load(); }, [page]);
  // Смена фильтров: возвращаемся на 1-ю страницу; если уже на ней —
  // загружаем напрямую (setPage(1) не вызвал бы эффект выше).
  useEffect(() => {
    if (page !== 1) setPage(1);
    else load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user, action, from, to]);

  const exportUrl = (ext: string) => {
    const usp = new URLSearchParams({ token: getToken() || "" });
    if (user) usp.set("username", user);
    if (action) usp.set("action", action);
    if (from) usp.set("date_from", from);
    if (to) usp.set("date_to", to);
    return `/api/audit/export.${ext}?${usp.toString()}`;
  };

  const statusColor = (s: number) =>
    s < 300 ? "var(--green)" : s < 400 ? "var(--accent)" : s < 500 ? "var(--orange)" : "var(--red)";

  return (
    <div>
      <h2>Журнал действий</h2>
      <div className="toolbar">
        <input placeholder="Пользователь" value={user} onChange={e => setUser(e.target.value)} style={{ width: 180 }} />
        <input placeholder="Действие (подстрока)" value={action} onChange={e => setAction(e.target.value)} style={{ width: 240 }} />
        <input type="datetime-local" value={from} onChange={e => setFrom(e.target.value)} />
        <input type="datetime-local" value={to} onChange={e => setTo(e.target.value)} />
        <a className="btn" href={exportUrl("xlsx")}>Excel</a>
        <a className="btn secondary" href={exportUrl("csv")}>CSV</a>
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
        <Pager page={page} pageSize={PAGE_SIZE} total={total} onPage={setPage} />
      </div>
    </div>
  );
}

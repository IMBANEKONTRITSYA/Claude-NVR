import { useEffect, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, LineChart, Line, CartesianGrid } from "recharts";
import { api } from "../api";

export function Dashboard() {
  const [kpi, setKpi] = useState<any>({});
  const [byDay, setByDay] = useState<any[]>([]);
  const [byHour, setByHour] = useState<any[]>([]);
  const [top, setTop] = useState<any[]>([]);

  useEffect(() => {
    api.kpi().then(setKpi).catch(() => {});
    api.byDay().then(setByDay).catch(() => {});
    api.byHour().then(setByHour).catch(() => {});
    api.topPersons().then(setTop).catch(() => {});
  }, []);

  const Tile = ({ label, value }: any) => (
    <div className="card" style={{ flex: 1, minWidth: 180 }}>
      <div className="muted">{label}</div>
      <div className="kpi">{value ?? 0}</div>
    </div>
  );

  return (
    <div>
      <h2>Дашборд</h2>
      <div className="row" style={{ marginBottom: 16 }}>
        <Tile label="Обнаружений сегодня" value={kpi.detections_today} />
        <Tile label="Уникальных персон сегодня" value={kpi.unique_persons_today} />
        <Tile label="Всего персон в базе" value={kpi.total_persons} />
        <Tile label="Известных персон" value={kpi.known_persons} />
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", marginBottom: 16 }}>
        <div className="card">
          <h3>Посещаемость по дням</h3>
          <ResponsiveContainer width="100%" height={240}>
            <LineChart data={byDay}>
              <CartesianGrid stroke="#30363d" />
              <XAxis dataKey="day" stroke="#8b949e" />
              <YAxis stroke="#8b949e" />
              <Tooltip contentStyle={{ background: "#1c2128", border: "1px solid #30363d" }} />
              <Line type="monotone" dataKey="count" stroke="#2f81f7" />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <div className="card">
          <h3>Часы пик (последние 7 дней)</h3>
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={byHour}>
              <CartesianGrid stroke="#30363d" />
              <XAxis dataKey="hour" stroke="#8b949e" />
              <YAxis stroke="#8b949e" />
              <Tooltip contentStyle={{ background: "#1c2128", border: "1px solid #30363d" }} />
              <Bar dataKey="count" fill="#3fb950" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="card">
        <h3>Топ-10 персон за 30 дней</h3>
        <table>
          <thead><tr><th>#</th><th>Имя</th><th>Статус</th><th>Обнаружений</th></tr></thead>
          <tbody>
            {top.map((p, i) => (
              <tr key={p.id}>
                <td>{i + 1}</td><td>{p.name}</td><td>{p.status}</td><td>{p.count}</td>
              </tr>
            ))}
            {top.length === 0 && <tr><td colSpan={4} className="empty">Нет данных</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

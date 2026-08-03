import { useEffect, useState } from "react";
import { api, getToken } from "../api";
import { getRole } from "../api";

function Bar({ percent, warn = 75, crit = 90 }: { percent: number; warn?: number; crit?: number }) {
  const color = percent >= crit ? "var(--red)" : percent >= warn ? "var(--orange)" : "var(--green)";
  return (
    <div style={{ background: "var(--bg)", borderRadius: 4, height: 8, overflow: "hidden", marginTop: 4 }}>
      <div style={{ width: `${Math.min(100, percent)}%`, height: "100%", background: color, transition: "width .3s" }} />
    </div>
  );
}

function Metric({ label, value, percent, hint }: any) {
  return (
    <div className="card" style={{ flex: 1, minWidth: 200 }}>
      <div className="muted">{label}</div>
      <div className="kpi" style={{ fontSize: 24 }}>{value}</div>
      {percent !== undefined && <Bar percent={percent} />}
      {hint && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

export function Monitoring() {
  const [m, setM] = useState<any>(null);
  const [err, setErr] = useState("");
  const isAdmin = getRole() === "admin";

  useEffect(() => {
    const tick = () => api.sysMetrics().then(r => { setM(r); setErr(""); }).catch(e => setErr(e.message));
    tick();
    const t = setInterval(tick, 5000);
    return () => clearInterval(t);
  }, []);

  if (err) return <div><h2>Мониторинг</h2><div className="empty">{err}</div></div>;
  if (!m) return <div><h2>Мониторинг</h2><div className="empty">Загрузка...</div></div>;

  const fps = Object.entries(m.camera_fps || {});

  return (
    <div>
      <h2>Мониторинг системы</h2>

      <div className="row" style={{ marginBottom: 16 }}>
        <Metric label="Загрузка CPU" value={`${m.cpu_percent}%`} percent={m.cpu_percent} />
        <Metric label="Оперативная память" value={`${m.ram_percent}%`} percent={m.ram_percent}
          hint={`${m.ram_used_mb} / ${m.ram_total_mb} МБ`} />
        <Metric label="Диск архива" value={`${m.disk_percent}%`} percent={m.disk_percent}
          hint={`свободно ${m.disk_free_gb} ГБ из ${m.disk_total_gb} ГБ`} />
        {m.temperature_c != null && <Metric label="Температура" value={`${m.temperature_c}°C`} />}
      </div>

      <div className="row" style={{ marginBottom: 16 }}>
        <Metric label="Камеры в сети" value={`${m.cameras_online} / ${m.cameras_enabled}`}
          hint={`всего заведено: ${m.cameras_total}`} />
        <Metric label="Событий сегодня" value={m.events_today} />
        <Metric label="Сегментов в архиве" value={m.segments_total} />
        <Metric label="Очередь апскейла" value={m.upscale_queue}
          hint={m.redis_ok ? "Redis доступен" : "Redis недоступен"} />
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>FPS детекции по камерам</h3>
        {fps.length === 0 && <div className="empty">Нет данных — воркер ещё не публиковал метрики</div>}
        {fps.length > 0 && (
          <table>
            <thead><tr><th>Камера</th><th>FPS детекции</th></tr></thead>
            <tbody>
              {fps.map(([id, v]: any) => (
                <tr key={id}>
                  <td>#{id}</td>
                  <td style={{ color: v < 1 ? "var(--orange)" : "var(--green)", fontWeight: 600 }}>{v}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {isAdmin && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Prometheus</h3>
          <p className="muted">Метрики в формате Prometheus для внешнего мониторинга (Grafana).</p>
          <a className="btn secondary" href={`/api/system/prometheus`} target="_blank" rel="noreferrer">
            Открыть /api/system/prometheus
          </a>
        </div>
      )}
    </div>
  );
}

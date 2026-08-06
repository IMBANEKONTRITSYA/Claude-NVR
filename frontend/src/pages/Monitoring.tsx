import { useEffect, useState } from "react";
import { api, getToken, getRole } from "../api";

function Bar({ percent, warn, crit }: { percent: number; warn?: number; crit?: number }) {
  // Умолчания через ?? , а не в сигнатуре: вызывающие передают warn/crit
  // из настроек, и явный undefined (настройка ещё не загрузилась) должен
  // падать на умолчание, а не красить полосу по NaN.
  const w = warn ?? 75;
  const c = crit ?? 90;
  const color = percent >= c ? "var(--red)" : percent >= w ? "var(--orange)" : "var(--green)";
  return (
    <div style={{ background: "var(--bg)", borderRadius: 4, height: 8, overflow: "hidden", marginTop: 4 }}>
      <div style={{ width: `${Math.min(100, percent)}%`, height: "100%", background: color, transition: "width .3s" }} />
    </div>
  );
}

function Metric({ label, value, percent, hint, warn, crit }: any) {
  return (
    <div className="card" style={{ flex: 1, minWidth: 200 }}>
      <div className="muted">{label}</div>
      <div className="kpi" style={{ fontSize: 24 }}>{value}</div>
      {/* Пороги пробрасываются в полосу: у диска архива они настраиваемые
          (SPEC §14), и полоса обязана краснеть на том же значении, на
          котором приходит алерт, а не на умолчании компонента. */}
      {percent !== undefined && <Bar percent={percent} warn={warn} crit={crit} />}
      {hint && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

/** Слой записи: статус каждого из 120 потоков (SPEC §14, §9). */
function RecordLayerPanel() {
  const [d, setD] = useState<any>(null);
  const [err, setErr] = useState("");
  // На 120 камерах таблица целиком нечитаема, а интересны почти всегда
  // проблемные потоки — поэтому фильтр, и по умолчанию он на них.
  const [filter, setFilter] = useState<"problems" | "all">("problems");

  useEffect(() => {
    const tick = () => api.recordLayer().then(r => { setD(r); setErr(""); }).catch(e => setErr(e.message));
    tick();
    const t = setInterval(tick, 10000);
    return () => clearInterval(t);
  }, []);

  if (err) return <div className="card" style={{ marginBottom: 16 }}><h3 style={{ marginTop: 0 }}>Слой записи</h3><div className="empty">{err}</div></div>;
  if (!d) return null;

  const s = d.summary;
  const gaps: number[] = d.segment_gaps || [];
  const streams: any[] = d.streams || [];
  const problems = streams.filter(x => x.status !== "online" || gaps.includes(x.camera_id));
  const shown = filter === "problems" ? problems : streams;

  const color = (st: string) =>
    st === "online" ? "var(--green)" : st === "offline" ? "var(--red)" : "var(--orange)";
  const label = (st: string) =>
    st === "online" ? "пишется" : st === "offline" ? "потерян" : "неизвестно";

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h3 style={{ marginTop: 0 }}>Слой записи</h3>

      {!d.available ? (
        <div className="empty">
          {d.reason || "Нет данных"} — за сутки записано {d.segments_last_day} сегментов
          ({d.gb_last_day} ГБ), камер включено {d.cameras_enabled}.
        </div>
      ) : (
        <>
          <div className="row">
            <Metric label="Потоков пишется" value={`${s.streams_online} / ${s.streams_total}`}
              percent={s.streams_total ? (s.streams_online * 100) / s.streams_total : 0}
              warn={101} crit={102}
              hint={s.streams_unknown
                ? `${s.streams_unknown} — состояние неизвестно (нет связи с медиасервером)`
                : `потеряно ${s.streams_offline}`} />
            <Metric label="Сегментов за сутки" value={d.segments_last_day}
              hint={`${d.gb_last_day} ГБ записано`} />
            <Metric label="Пропусков записи" value={gaps.length}
              hint={gaps.length ? `камеры: ${gaps.join(", ")}` : "нет"} />
            <Metric label="Кадров с ошибками" value={s.frames_in_error}
              hint="суммарно по всем потокам" />
          </div>

          {gaps.length > 0 && (
            <div style={{
              padding: "8px 12px", borderRadius: 4, marginTop: 12,
              background: "var(--red)", color: "#fff", fontWeight: 600,
            }}>
              Пропуск записи сегментов на камерах: {gaps.join(", ")} — поток есть,
              но файлы не пишутся. Проверьте место на диске и права на каталог архива.
            </div>
          )}

          <div style={{ marginTop: 12, marginBottom: 8 }}>
            <button className={filter === "problems" ? "btn" : "btn secondary"}
              onClick={() => setFilter("problems")}>
              Проблемные ({problems.length})
            </button>
            <button className={filter === "all" ? "btn" : "btn secondary"}
              style={{ marginLeft: 8 }} onClick={() => setFilter("all")}>
              Все потоки ({streams.length})
            </button>
          </div>

          {shown.length === 0 ? (
            <div className="empty">
              {filter === "problems" ? "Все потоки пишутся нормально" : "Потоков нет"}
            </div>
          ) : (
            <div style={{ maxHeight: 320, overflowY: "auto" }}>
              <table>
                <thead>
                  <tr><th>Камера</th><th>Статус</th><th>Принято</th><th>Ошибки кадров</th><th>В сети с</th></tr>
                </thead>
                <tbody>
                  {shown.map(x => (
                    <tr key={x.camera_id}>
                      <td>#{x.camera_id} {x.name}</td>
                      <td style={{ color: color(x.status), fontWeight: 600 }}>
                        {label(x.status)}
                        {gaps.includes(x.camera_id) && " · пропуск сегмента"}
                      </td>
                      <td>{(x.inbound_bytes / 1048576).toFixed(1)} МБ</td>
                      <td style={{ color: x.frames_in_error ? "var(--orange)" : undefined }}>
                        {x.frames_in_error}
                      </td>
                      <td className="muted">
                        {x.online_since ? new Date(x.online_since).toLocaleString("ru-RU") : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** Калькулятор хранения (SPEC §21): битрейт × камеры × дни → требуемый объём. */
function StorageCalculator() {
  const [f, setF] = useState({ bitrate_kbps: 2048, cameras: 120, days: 14 });
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState("");

  const calc = () => api.storageCalc(f.bitrate_kbps, f.cameras, f.days)
    .then(r => { setRes(r); setErr(""); })
    .catch(e => { setRes(null); setErr(e.message); });

  useEffect(() => { calc(); }, []);

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h3 style={{ marginTop: 0 }}>Калькулятор хранения</h3>
      <p className="muted" style={{ marginTop: 0 }}>
        Сколько диска нужно под заданную глубину архива. Формула ТЗ: Мбит/с × 10.8 = ГБ/сутки на камеру.
      </p>
      <div className="grid" style={{ gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
        <div>
          <label>Битрейт основного потока, кбит/с</label>
          <input type="number" min={64} max={100000} value={f.bitrate_kbps}
            onChange={e => setF({ ...f, bitrate_kbps: Number(e.target.value) })} />
        </div>
        <div>
          <label>Камер</label>
          <input type="number" min={1} max={1000} value={f.cameras}
            onChange={e => setF({ ...f, cameras: Number(e.target.value) })} />
        </div>
        <div>
          <label>Глубина хранения, суток</label>
          <input type="number" min={1} max={3650} value={f.days}
            onChange={e => setF({ ...f, days: Number(e.target.value) })} />
        </div>
      </div>
      <button className="btn" style={{ marginTop: 8 }} onClick={calc}>Рассчитать</button>
      {err && <div className="empty" style={{ marginTop: 8 }}>{err}</div>}
      {res && (
        <div className="row" style={{ marginTop: 12 }}>
          <Metric label="На камеру" value={`${res.gb_per_day_per_camera} ГБ/сут`} />
          <Metric label="Суммарно" value={`${res.gb_per_day_total} ГБ/сут`} />
          <Metric label="Требуется" value={`${res.required_tb} ТБ`}
            hint={`${res.required_gb} ГБ на ${res.days} сут`} />
        </div>
      )}
    </div>
  );
}

/** Хранилище архива: заполнение, фактический расход, прогноз (SPEC §5, §21). */
function StoragePanel({ isAdmin }: { isAdmin: boolean }) {
  const [s, setS] = useState<any>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    const tick = () => api.storage().then(r => { setS(r); setErr(""); }).catch(e => setErr(e.message));
    tick();
    const t = setInterval(tick, 15000);
    return () => clearInterval(t);
  }, []);

  if (err) return <div className="card" style={{ marginBottom: 16 }}><h3 style={{ marginTop: 0 }}>Хранилище</h3><div className="empty">{err}</div></div>;
  if (!s) return null;

  const overrides = Object.entries(s.per_camera_retention || {});
  // Прочерк вместо числа, пока расход не измерен: см. days_left() в
  // services/storage.py — «бесконечность» в первые минуты была бы враньём.
  const left = s.days_left == null ? "—" : `${s.days_left} сут`;
  const alertText = s.alert_level === "critical"
    ? `Диск заполнен более чем на ${s.crit_percent}% — старейшие сегменты будут удалены автоматически`
    : s.alert_level === "warning"
      ? `Диск заполнен более чем на ${s.warn_percent}%`
      : "";

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h3 style={{ marginTop: 0 }}>Хранилище архива</h3>
      {alertText && (
        <div style={{
          padding: "8px 12px", borderRadius: 4, marginBottom: 12,
          background: s.alert_level === "critical" ? "var(--red)" : "var(--orange)",
          color: "#fff", fontWeight: 600,
        }}>{alertText}</div>
      )}
      <div className="row">
        <Metric label="Заполнение диска" value={`${s.disk_used_percent}%`}
          percent={s.disk_used_percent} warn={s.warn_percent} crit={s.crit_percent}
          hint={`свободно ${s.disk_free_gb} ГБ из ${s.disk_total_gb} ГБ`} />
        <Metric label="Хватит места на" value={left}
          hint={s.forecast_source === "measured"
            ? `по фактическому расходу ${s.measured_gb_per_day} ГБ/сут`
            : `расчётно, ${s.nominal_gb_per_day} ГБ/сут — фактических данных пока нет`} />
        <Metric label="Расход за сутки" value={`${s.measured_gb_per_day} ГБ`}
          hint={`расчётный ${s.nominal_gb_per_day} ГБ · сегментов ${s.segments_last_day}`} />
        <Metric label="Объём архива" value={`${s.archive_gb} ГБ`}
          hint={`глубина хранения ${s.retention_days} сут · камер на записи ${s.cameras_recording}`} />
      </div>
      {s.calibration != null && (
        <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
          Калибровка: фактический расход составляет {Math.round(s.calibration * 100)}% от расчётного
          {s.calibration < 1 ? " (VBR и smart-кодек экономят)" : " (выше расчёта — проверьте битрейт камер)"}.
        </div>
      )}
      {overrides.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>
            Камеры с собственной глубиной хранения:
          </div>
          <div>{overrides.map(([id, d]: any) => (
            <span key={id} className="badge" style={{ marginRight: 6 }}>#{id}: {d} сут</span>
          ))}</div>
        </div>
      )}
      {isAdmin && (
        <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
          Глобальная глубина хранения и пороги алертов настраиваются в разделе «Настройки».
        </div>
      )}
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

      <RecordLayerPanel />
      <StoragePanel isAdmin={isAdmin} />
      {isAdmin && <StorageCalculator />}

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
          <a className="btn secondary" href={`/api/system/prometheus?token=${getToken()}`} target="_blank" rel="noreferrer">
            Открыть /api/system/prometheus
          </a>
        </div>
      )}
    </div>
  );
}

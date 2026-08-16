import { useEffect, useState } from "react";
import { api, getToken } from "../api";

export function Archive() {
  const [cams, setCams] = useState<any[]>([]);
  const [segs, setSegs] = useState<any[]>([]);
  const [f, setF] = useState({ camera_id: "", event_type: "", date_from: "", date_to: "", person_id: "" });
  const [sel, setSel] = useState<any | null>(null);
  // Границы экспортируемого фрагмента (ТЗ §5). Держатся отдельно от
  // фильтров поиска: оператор ищет по часам, а выгружает минуты.
  const [exp, setExp] = useState({ from: "", to: "" });

  useEffect(() => { api.cameras().then(setCams); search(); }, []);

  // Выбор сегмента подставляет его границы как начальное окно экспорта —
  // дальше оператор сужает их до нужного события.
  //
  // Значения берутся из ответа API как есть и уходят обратно как есть, без
  // преобразования часовых поясов: столбцы `video_segments` наивные и
  // хранят UTC, и любой промежуточный `new Date()` сдвинул бы окно на
  // смещение пояса браузера — выгрузился бы не тот отрезок.
  useEffect(() => {
    if (!sel) return;
    setExp({ from: String(sel.started_at).slice(0, 19), to: String(sel.ended_at ?? "").slice(0, 19) });
  }, [sel]);

  const search = async () => {
    const params: Record<string, string> = {};
    Object.entries(f).forEach(([k, v]) => { if (v) params[k] = v; });
    setSegs(await api.archive(params));
  };

  const url = (id: number) => `/api/archive/file/${id}`;

  const exportUrl = (cameraId: number, from: string, to: string) => {
    const q = new URLSearchParams({
      camera_id: String(cameraId),
      date_from: from,
      date_to: to,
      token: getToken() ?? "",
    });
    return `/api/archive/export?${q}`;
  };

  return (
    <div>
      <h2>Архив</h2>
      <div className="card" style={{ marginBottom: 16 }}>
        <div className="grid" style={{ gridTemplateColumns: "repeat(4, 1fr)", gap: 8 }}>
          <div>
            <label>Камера</label>
            <select value={f.camera_id} onChange={e => setF({ ...f, camera_id: e.target.value })}>
              <option value="">Все</option>
              {cams.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
          <div>
            <label>Тип</label>
            {/* Запись непрерывная (SPEC §5): с цикла 24 все новые сегменты
                идут с типом "continuous". "Движение"/"Лицо" оставлены для
                записей, сделанных прежним событийным слоем записи, — архив
                смешанный, пока они не выйдут за retention. */}
            <select value={f.event_type} onChange={e => setF({ ...f, event_type: e.target.value })}>
              <option value="">Все</option>
              <option value="continuous">Непрерывная запись</option>
              <option value="motion">Движение (архив до обновления)</option>
              <option value="face">Лицо (архив до обновления)</option>
            </select>
          </div>
          <div><label>С</label><input type="datetime-local" value={f.date_from} onChange={e => setF({ ...f, date_from: e.target.value })} /></div>
          <div><label>По</label><input type="datetime-local" value={f.date_to} onChange={e => setF({ ...f, date_to: e.target.value })} /></div>
        </div>
        <div style={{ marginTop: 8 }}>
          <label>ID персоны (опционально)</label>
          <input type="number" value={f.person_id} onChange={e => setF({ ...f, person_id: e.target.value })} style={{ width: 200 }} />
        </div>
        <button className="btn" style={{ marginTop: 8 }} onClick={search}>Найти</button>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 1.4fr", gap: 16 }}>
        <div className="card">
          <h3>Найдено: {segs.length}</h3>
          <table>
            <thead><tr><th>Время</th><th>Камера</th><th>Тип</th><th>Длит.</th></tr></thead>
            <tbody>
              {segs.map(s => (
                <tr key={s.id} style={{ cursor: "pointer", background: sel?.id === s.id ? "#222" : undefined }} onClick={() => setSel(s)}>
                  <td>{new Date(s.started_at).toLocaleString("ru-RU")}</td>
                  <td>#{s.camera_id}</td>
                  <td>{s.event_type}</td>
                  <td>{s.duration_sec}с</td>
                </tr>
              ))}
              {segs.length === 0 && <tr><td colSpan={4} className="empty">Ничего не найдено</td></tr>}
            </tbody>
          </table>
        </div>
        <div className="card">
          {sel ? (
            <>
              <h3>Сегмент #{sel.id}</h3>
              <video controls style={{ width: "100%", background: "#000" }}
                src={`${url(sel.id)}?token=${getToken()}`} />
              <a className="btn" href={`${url(sel.id)}?token=${getToken()}`} download style={{ marginTop: 8, display: "inline-block" }}>
                Скачать MP4
              </a>

              {/* ТЗ §5 «экспорт фрагментов»: до этого архив умел отдавать
                  только сегмент целиком, и событие на границе двух
                  сегментов оператор склеивал вручную. Фрагмент собирается
                  remux'ом поверх скольких угодно сегментов камеры. */}
              <div style={{ marginTop: 16, borderTop: "1px solid #333", paddingTop: 12 }}>
                <h4 style={{ margin: "0 0 8px" }}>Экспорт фрагмента</h4>
                <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                  <div>
                    <label>С</label>
                    <input type="datetime-local" step="1" value={exp.from}
                           onChange={e => setExp({ ...exp, from: e.target.value })} />
                  </div>
                  <div>
                    <label>По</label>
                    <input type="datetime-local" step="1" value={exp.to}
                           onChange={e => setExp({ ...exp, to: e.target.value })} />
                  </div>
                </div>
                <a className="btn"
                   href={exportUrl(sel.camera_id, exp.from, exp.to)}
                   download
                   style={{
                     marginTop: 8, display: "inline-block",
                     // Пустые границы дали бы 422 от сервера; ссылка
                     // гасится до запроса.
                     pointerEvents: exp.from && exp.to ? undefined : "none",
                     opacity: exp.from && exp.to ? 1 : 0.5,
                   }}>
                  Скачать фрагмент
                </a>
                <div className="empty" style={{ marginTop: 6, fontSize: 12 }}>
                  Фрагмент склеивается из сегментов камеры без перекодирования,
                  поэтому начало сдвигается к ближайшему опорному кадру —
                  на 1–2 секунды раньше указанного.
                </div>
              </div>
            </>
          ) : <div className="empty">Выберите сегмент</div>}
        </div>
      </div>
    </div>
  );
}

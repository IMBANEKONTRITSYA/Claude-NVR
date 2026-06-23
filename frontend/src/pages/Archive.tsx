import { useEffect, useState } from "react";
import { api, getToken } from "../api";

export function Archive() {
  const [cams, setCams] = useState<any[]>([]);
  const [segs, setSegs] = useState<any[]>([]);
  const [f, setF] = useState({ camera_id: "", event_type: "", date_from: "", date_to: "" });
  const [sel, setSel] = useState<any | null>(null);

  useEffect(() => { api.cameras().then(setCams); search(); }, []);

  const search = async () => {
    const params: Record<string, string> = {};
    Object.entries(f).forEach(([k, v]) => { if (v) params[k] = v; });
    setSegs(await api.archive(params));
  };

  const url = (id: number) => `/api/archive/file/${id}`;

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
            <select value={f.event_type} onChange={e => setF({ ...f, event_type: e.target.value })}>
              <option value="">Все</option>
              <option value="motion">Движение</option>
              <option value="face">Лицо</option>
            </select>
          </div>
          <div><label>С</label><input type="datetime-local" value={f.date_from} onChange={e => setF({ ...f, date_from: e.target.value })} /></div>
          <div><label>По</label><input type="datetime-local" value={f.date_to} onChange={e => setF({ ...f, date_to: e.target.value })} /></div>
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
            </>
          ) : <div className="empty">Выберите сегмент</div>}
        </div>
      </div>
    </div>
  );
}

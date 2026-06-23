import { useEffect, useState } from "react";
import { api } from "../api";

export function Cameras() {
  const [cams, setCams] = useState<any[]>([]);
  const [form, setForm] = useState({ name: "", rtsp_url: "", location: "", enabled: true });
  const [editing, setEditing] = useState<number | null>(null);

  const load = () => api.cameras().then(setCams).catch(() => {});
  useEffect(() => { load(); }, []);

  const submit = async () => {
    try {
      if (editing) await api.camUpdate(editing, form);
      else await api.camAdd(form);
      setForm({ name: "", rtsp_url: "", location: "", enabled: true });
      setEditing(null);
      load();
    } catch (e: any) { alert(e.message); }
  };

  const remove = async (id: number) => {
    if (!confirm("Удалить камеру?")) return;
    await api.camDelete(id);
    load();
  };

  return (
    <div>
      <h2>Управление камерами</h2>
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>{editing ? "Редактирование" : "Добавить камеру"}</h3>
        <div className="grid" style={{ gridTemplateColumns: "1fr 2fr 1fr", marginBottom: 8 }}>
          <div><label>Название</label><input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></div>
          <div><label>RTSP URL</label><input value={form.rtsp_url} onChange={e => setForm({ ...form, rtsp_url: e.target.value })} placeholder="rtsp://user:pass@ip:554/stream" /></div>
          <div><label>Локация</label><input value={form.location} onChange={e => setForm({ ...form, location: e.target.value })} /></div>
        </div>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6, marginRight: 12 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={form.enabled} onChange={e => setForm({ ...form, enabled: e.target.checked })} />
          Активна
        </label>
        <button className="btn" onClick={submit}>{editing ? "Сохранить" : "Добавить"}</button>
        {editing && <button className="btn secondary" onClick={() => { setEditing(null); setForm({ name: "", rtsp_url: "", location: "", enabled: true }); }} style={{ marginLeft: 8 }}>Отмена</button>}
      </div>

      <div className="card">
        <table>
          <thead><tr><th>ID</th><th>Название</th><th>Локация</th><th>Статус</th><th>Активна</th><th></th></tr></thead>
          <tbody>
            {cams.map(c => (
              <tr key={c.id}>
                <td>{c.id}</td>
                <td>{c.name}</td>
                <td>{c.location}</td>
                <td><span className={`badge ${c.status}`}>{c.status}</span></td>
                <td>{c.enabled ? "да" : "нет"}</td>
                <td>
                  <button className="btn secondary" onClick={() => { setEditing(c.id); setForm({ name: c.name, rtsp_url: "", location: c.location, enabled: c.enabled }); }}>Изм.</button>
                  <button className="btn danger" onClick={() => remove(c.id)} style={{ marginLeft: 4 }}>Удалить</button>
                </td>
              </tr>
            ))}
            {cams.length === 0 && <tr><td colSpan={6} className="empty">Камер нет</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

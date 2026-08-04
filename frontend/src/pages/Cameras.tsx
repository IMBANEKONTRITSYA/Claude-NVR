import { useEffect, useState } from "react";
import { api } from "../api";
import { useUI } from "../ui";

const EMPTY_FORM = {
  name: "", rtsp_url: "", sub_rtsp_url: "", location: "", enabled: true,
  onvif_enabled: false, onvif_host: "", onvif_port: 80, onvif_username: "", onvif_password: "",
};

export function Cameras() {
  const { toast, confirm } = useUI();
  const [cams, setCams] = useState<any[]>([]);
  const [form, setForm] = useState(EMPTY_FORM);
  const [editing, setEditing] = useState<number | null>(null);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string>("");

  const load = () => api.cameras().then(setCams).catch(() => {});
  useEffect(() => { load(); }, []);

  const submit = async () => {
    try {
      if (editing) await api.camUpdate(editing, form);
      else await api.camAdd(form);
      setForm(EMPTY_FORM);
      setEditing(null);
      load();
      toast(editing ? "Камера обновлена" : "Камера добавлена", "ok");
    } catch (e: any) { toast(e.message, "err"); }
  };

  const remove = async (id: number) => {
    if (!(await confirm("Удалить камеру?"))) return;
    try {
      await api.camDelete(id);
      load();
      toast("Камера удалена", "ok");
    } catch (e: any) { toast(e.message, "err"); }
  };

  return (
    <div>
      <h2>Управление камерами</h2>
      <div className="card" style={{ marginBottom: 16 }}>
        <h3>{editing ? "Редактирование" : "Добавить камеру"}</h3>
        <div className="grid" style={{ gridTemplateColumns: "1fr 2fr 1fr", marginBottom: 8 }}>
          <div><label>Название</label><input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} /></div>
          <div><label>RTSP URL</label><input value={form.rtsp_url} onChange={e => setForm({ ...form, rtsp_url: e.target.value })} placeholder="rtsp://user:pass@ip:554/stream" /></div>
          <div><label>RTSP субпотока (для детекции)</label><input value={form.sub_rtsp_url} onChange={e => setForm({ ...form, sub_rtsp_url: e.target.value })} placeholder="640x360, необязательно" /></div>
          <div><label>Локация</label><input value={form.location} onChange={e => setForm({ ...form, location: e.target.value })} /></div>
        </div>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 6, marginRight: 12 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={form.enabled} onChange={e => setForm({ ...form, enabled: e.target.checked })} />
          Активна
        </label>
        <button className="btn" onClick={submit}>{editing ? "Сохранить" : "Добавить"}</button>
        <button className="btn secondary" style={{ marginLeft: 8 }} disabled={!form.rtsp_url || testing}
          onClick={async () => {
            setTesting(true); setTestResult("");
            try {
              const r = await api.testRtsp(form.rtsp_url);
              setTestResult(r.ok ? `OK — ${r.info?.split("\n")[0] || "поток доступен"}` : `Ошибка: ${r.error}`);
            } catch (e: any) { setTestResult(`Ошибка: ${e.message}`); }
            finally { setTesting(false); }
          }}>{testing ? "Проверка..." : "Проверить RTSP"}</button>
        {editing && <button className="btn secondary" onClick={() => { setEditing(null); setForm(EMPTY_FORM); setTestResult(""); }} style={{ marginLeft: 8 }}>Отмена</button>}
        {testResult && <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>{testResult}</div>}

        <div style={{ marginTop: 16, paddingTop: 12, borderTop: "1px solid var(--border)" }}>
          <label style={{ display: "inline-flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
            <input type="checkbox" style={{ width: "auto" }} checked={form.onvif_enabled}
              onChange={e => setForm({ ...form, onvif_enabled: e.target.checked })} />
            ONVIF-события движения (вместо постоянного анализа кадров)
          </label>
          {form.onvif_enabled && (
            <div className="grid" style={{ gridTemplateColumns: "2fr 1fr 1fr 1fr" }}>
              <div><label>ONVIF-адрес камеры</label><input value={form.onvif_host}
                onChange={e => setForm({ ...form, onvif_host: e.target.value })} placeholder="192.168.1.64" /></div>
              <div><label>Порт</label><input type="number" value={form.onvif_port}
                onChange={e => setForm({ ...form, onvif_port: +e.target.value })} /></div>
              <div><label>Логин</label><input value={form.onvif_username}
                onChange={e => setForm({ ...form, onvif_username: e.target.value })} /></div>
              <div><label>Пароль{editing ? " (оставить пустым — не менять)" : ""}</label>
                <input type="password" value={form.onvif_password}
                  onChange={e => setForm({ ...form, onvif_password: e.target.value })} /></div>
            </div>
          )}
        </div>
      </div>

      <div className="card">
        <table>
          <thead><tr><th>ID</th><th>Название</th><th>Локация</th><th>Статус</th><th>Активна</th><th></th></tr></thead>
          <tbody>
            {cams.map(c => (
              <tr key={c.id}>
                <td>{c.id}</td>
                <td>
                  {c.name}
                  {c.has_substream && <span className="muted" style={{ fontSize: 10, marginLeft: 6 }} title="Детекция идёт по субпотоку">SUB</span>}
                  {c.onvif_enabled && <span className="muted" style={{ fontSize: 10, marginLeft: 6 }} title="Движение — по событиям ONVIF">ONVIF</span>}
                </td>
                <td>{c.location}</td>
                <td><span className={`badge ${c.status}`}>{c.status}</span></td>
                <td>
                  <label style={{ display: "inline-flex", alignItems: "center", gap: 4, cursor: "pointer" }}>
                    <input type="checkbox" style={{ width: "auto" }} checked={c.enabled} onChange={async e => {
                      try { await api.camToggle(c.id, e.target.checked); load(); toast(e.target.checked ? "Камера включена" : "Камера отключена", "ok"); }
                      catch (err: any) { toast(err.message, "err"); }
                    }} />
                    {c.enabled ? "да" : "нет"}
                  </label>
                </td>
                <td>
                  <button className="btn secondary" onClick={() => {
                    setEditing(c.id);
                    setForm({
                      name: c.name, rtsp_url: "", sub_rtsp_url: "", location: c.location, enabled: c.enabled,
                      onvif_enabled: !!c.onvif_enabled, onvif_host: "", onvif_port: 80, onvif_username: "", onvif_password: "",
                    });
                  }}>Изм.</button>
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

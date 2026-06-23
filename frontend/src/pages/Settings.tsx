import { useEffect, useState } from "react";
import { api } from "../api";

export function Settings() {
  const [s, setS] = useState<any>({});
  const [msg, setMsg] = useState<{ type: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api.getSettings().then(setS).catch(() => {});
  useEffect(() => { load(); }, []);

  const save = async () => {
    setBusy(true); setMsg(null);
    try {
      const payload = {
        retention_days: parseInt(s.retention_days),
        motion_threshold: parseInt(s.motion_threshold),
        similarity_threshold: parseFloat(s.similarity_threshold),
        detection_fps: parseInt(s.detection_fps),
      };
      const r = await api.putSettings(payload);
      setS(r);
      setMsg({ type: "ok", text: "Сохранено. Воркер применит изменения в течение ~10 секунд." });
    } catch (e: any) { setMsg({ type: "err", text: e.message }); }
    finally { setBusy(false); }
  };

  const Field = ({ k, label, hint, step }: any) => (
    <div style={{ marginBottom: 14 }}>
      <label>{label}</label>
      <input type="number" step={step || 1} value={s[k] ?? ""} onChange={e => setS({ ...s, [k]: e.target.value })} />
      {hint && <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>{hint}</div>}
    </div>
  );

  return (
    <div>
      <h2>Системные настройки</h2>
      <div className="card" style={{ maxWidth: 520 }}>
        <Field k="retention_days" label="Глубина хранения архива (дней)" hint="После — видео и события удаляются автоматически (циклическая перезапись)" />
        <Field k="detection_fps" label="Частота детекции (FPS на канал)" hint="Рекомендуется 5; выше — больше нагрузка на CPU" />
        <Field k="motion_threshold" label="Порог движения (пикселей)" hint="Чувствительность детектора движения; меньше — чувствительнее" />
        <Field k="similarity_threshold" label="Порог схожести лиц" step={0.05} hint="0.1–0.9; меньше значение → строже сопоставление с известными персонами" />
        {msg && <div style={{ marginBottom: 10, color: msg.type === "ok" ? "var(--green)" : "var(--red)" }}>{msg.text}</div>}
        <button className="btn" onClick={save} disabled={busy}>{busy ? "Сохранение..." : "Сохранить"}</button>
      </div>
    </div>
  );
}

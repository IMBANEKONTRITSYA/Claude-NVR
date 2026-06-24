import { useEffect, useState } from "react";
import { api } from "../api";
import { useUI } from "../ui";

export function Settings() {
  const { toast } = useUI();
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
        alert_cooldown_sec: parseInt(s.alert_cooldown_sec),
        telegram_bot_token: s.telegram_bot_token || "",
        telegram_chat_id: s.telegram_chat_id || "",
      };
      const r = await api.putSettings(payload);
      setS(r);
      setMsg({ type: "ok", text: "Сохранено. Воркер применит изменения в течение ~10 секунд." });
    } catch (e: any) { setMsg({ type: "err", text: e.message }); }
    finally { setBusy(false); }
  };

  const testTg = async () => {
    try {
      await api.testTelegram();
      toast("Тестовое сообщение отправлено", "ok");
    } catch (e: any) { toast(e.message, "err"); }
  };

  const Field = ({ k, label, hint, step, type = "number" }: any) => (
    <div style={{ marginBottom: 14 }}>
      <label>{label}</label>
      <input type={type} step={step || 1} value={s[k] ?? ""} onChange={e => setS({ ...s, [k]: e.target.value })} />
      {hint && <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>{hint}</div>}
    </div>
  );

  return (
    <div>
      <h2>Системные настройки</h2>
      <div className="card" style={{ maxWidth: 520, marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Детекция и хранение</h3>
        <Field k="retention_days" label="Глубина хранения архива (дней)" hint="После — видео и события удаляются автоматически" />
        <Field k="detection_fps" label="Частота детекции (FPS на канал)" hint="Рекомендуется 5; выше — больше нагрузка на CPU" />
        <Field k="motion_threshold" label="Порог движения (пикселей)" hint="Чувствительность детектора движения; меньше — чувствительнее" />
        <Field k="similarity_threshold" label="Порог схожести лиц" step={0.05} hint="0.1–0.9; меньше → строже сопоставление с известными" />
      </div>
      <div className="card" style={{ maxWidth: 520, marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Telegram-оповещения (watchlist)</h3>
        <Field k="telegram_bot_token" type="text" label="Bot token" hint="Создаётся через @BotFather; оставьте пустым, чтобы отключить оповещения" />
        <Field k="telegram_chat_id" type="text" label="Chat ID" hint="ID чата или канала, куда отправлять оповещения" />
        <Field k="alert_cooldown_sec" label="Cooldown между оповещениями (сек)" hint="Чтобы не спамить, для одной персоны не чаще раза за указанный интервал" />
        <button className="btn secondary" onClick={testTg}>Отправить тестовое сообщение</button>
      </div>
      <div style={{ maxWidth: 520 }}>
        {msg && <div style={{ marginBottom: 10, color: msg.type === "ok" ? "var(--green)" : "var(--red)" }}>{msg.text}</div>}
        <button className="btn" onClick={save} disabled={busy}>{busy ? "Сохранение..." : "Сохранить"}</button>
      </div>
    </div>
  );
}

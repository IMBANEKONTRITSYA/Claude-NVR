import { useEffect, useState } from "react";
import { api } from "../api";
import { useUI } from "../ui";

// ВАЖНО: Field объявлен вне Settings — компонент, объявленный внутри рендера,
// пересоздаётся как новый тип на каждый ре-рендер, из-за чего input
// размонтируется и теряет фокус после каждого введённого символа.
function Field({ label, hint, value, onChange, step, type = "number" }: any) {
  return (
    <div style={{ marginBottom: 14 }}>
      <label>{label}</label>
      <input type={type} step={step || 1} value={value ?? ""} onChange={onChange} />
      {hint && <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

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
        event_cooldown_sec: parseInt(s.event_cooldown_sec),
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

  const upd = (k: string) => (e: any) => setS((prev: any) => ({ ...prev, [k]: e.target.value }));

  return (
    <div>
      <h2>Системные настройки</h2>
      <div className="card" style={{ maxWidth: 520, marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Детекция и хранение</h3>
        <Field label="Глубина хранения архива (дней)" value={s.retention_days} onChange={upd("retention_days")} hint="После — видео и события удаляются автоматически" />
        <Field label="Частота детекции (FPS на канал)" value={s.detection_fps} onChange={upd("detection_fps")} hint="Рекомендуется 5; выше — больше нагрузка на CPU" />
        <Field label="Порог движения (пикселей)" value={s.motion_threshold} onChange={upd("motion_threshold")} hint="Чувствительность детектора движения; меньше — чувствительнее" />
        <Field label="Порог схожести лиц" value={s.similarity_threshold} onChange={upd("similarity_threshold")} step={0.05} hint="0.1–0.9; меньше → строже сопоставление с известными" />
        <Field label="Интервал между событиями (сек)" value={s.event_cooldown_sec} onChange={upd("event_cooldown_sec")} hint="Одна персона на одной камере создаёт событие не чаще раза за интервал (защита от спама на Стене и в БД)" />
      </div>
      <div className="card" style={{ maxWidth: 520, marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Telegram-оповещения (watchlist)</h3>
        <Field type="text" label="Bot token" value={s.telegram_bot_token} onChange={upd("telegram_bot_token")} hint="Создаётся через @BotFather; оставьте пустым, чтобы отключить оповещения" />
        <Field type="text" label="Chat ID" value={s.telegram_chat_id} onChange={upd("telegram_chat_id")} hint="ID чата или канала, куда отправлять оповещения" />
        <Field label="Cooldown между оповещениями (сек)" value={s.alert_cooldown_sec} onChange={upd("alert_cooldown_sec")} hint="Чтобы не спамить, для одной персоны не чаще раза за указанный интервал" />
        <button className="btn secondary" onClick={testTg}>Отправить тестовое сообщение</button>
      </div>
      <div style={{ maxWidth: 520 }}>
        {msg && <div style={{ marginBottom: 10, color: msg.type === "ok" ? "var(--green)" : "var(--red)" }}>{msg.text}</div>}
        <button className="btn" onClick={save} disabled={busy}>{busy ? "Сохранение..." : "Сохранить"}</button>
      </div>
    </div>
  );
}

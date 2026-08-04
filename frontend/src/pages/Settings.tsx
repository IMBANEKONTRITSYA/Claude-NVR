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

  const [titles, setTitles] = useState<Record<string, string>>({});

  const load = () => api.getSettings().then(setS).catch(() => {});
  useEffect(() => {
    load();
    api.listProfiles().then((r: any) => setTitles(r.titles || {})).catch(() => {});
  }, []);

  const applyProfile = async (name: string) => {
    try {
      const r = await api.applyProfile(name);
      setS(r);
      toast("Профиль применён — воркер подхватит настройки за ~10 секунд", "ok");
    } catch (e: any) { toast(e.message, "err"); }
  };

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
        frame_skip: parseInt(s.frame_skip),
        motion_prefilter: parseInt(s.motion_prefilter),
        idle_fps: parseInt(s.idle_fps),
        face_model: s.face_model,
        upscale_mode: s.upscale_mode,
        cluster_interval_min: parseInt(s.cluster_interval_min),
        detect_width: parseInt(s.detect_width),
        record_codec: s.record_codec,
        record_bitrate: parseInt(s.record_bitrate) || 0,
        record_iframe_only: parseInt(s.record_iframe_only) || 0,
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
        <h3 style={{ marginTop: 0 }}>Профиль производительности</h3>
        <div className="muted" style={{ fontSize: 12, marginBottom: 10 }}>
          Текущий: <b>{s.performance_profile === "custom" ? "своя настройка" : s.performance_profile || "—"}</b>.
          Профиль задаёт частоту детекции, пропуск кадров, модель распознавания,
          режим апскейла и интервал кластеризации. Любой параметр ниже можно изменить вручную.
        </div>
        {["economy", "standard", "maximum"].map(p => (
          <div key={p} style={{ marginBottom: 8 }}>
            <button className={`btn ${s.performance_profile === p ? "" : "secondary"}`}
              style={{ width: "100%", textAlign: "left" }} onClick={() => applyProfile(p)}>
              {titles[p] || p}
            </button>
          </div>
        ))}
      </div>

      <div className="card" style={{ maxWidth: 520, marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Оптимизация</h3>
        <Field label="Пропуск кадров" value={s.frame_skip} onChange={upd("frame_skip")} hint="0 — анализировать каждый кадр; 3 — каждый 4-й (меньше нагрузка на CPU)" />
        <Field label="FPS в режиме покоя" value={s.idle_fps} onChange={upd("idle_fps")} hint="Частота детекции, когда движения нет более 20 секунд" />
        <div style={{ marginBottom: 14 }}>
          <label>Префильтр движения</label>
          <select value={s.motion_prefilter ?? "1"} onChange={upd("motion_prefilter")}>
            <option value="1">Включён — детектор лиц только при движении</option>
            <option value="0">Выключен — анализировать каждый кадр</option>
          </select>
        </div>
        <div style={{ marginBottom: 14 }}>
          <label>Модель распознавания</label>
          <select value={s.face_model ?? "buffalo_s"} onChange={upd("face_model")}>
            <option value="buffalo_s">buffalo_s — лёгкая (слабое железо)</option>
            <option value="buffalo_l">buffalo_l — точная (мощное железо)</option>
          </select>
        </div>
        <Field label="Разрешение детекции (px)" value={s.detect_width} onChange={upd("detect_width")} hint="640 для слабого CPU, 960+ для мощного" />
        <div style={{ marginBottom: 14 }}>
          <label>Режим апскейла лиц</label>
          <select value={s.upscale_mode ?? "avatar"} onChange={upd("upscale_mode")}>
            <option value="manual">Только вручную — минимальная нагрузка</option>
            <option value="avatar">Аватары в фоне — улучшается первый снимок персоны</option>
            <option value="all">Вся галерея — максимальное качество</option>
          </select>
        </div>
        <Field label="Интервал кластеризации (мин)" value={s.cluster_interval_min} onChange={upd("cluster_interval_min")} hint="Пакетное объединение дублей неизвестных персон" />
        <div style={{ marginBottom: 14 }}>
          <label>Кодек записи архива</label>
          <select value={s.record_codec ?? "h264"} onChange={upd("record_codec")}>
            <option value="h264">H.264 — совместим со всеми браузерами</option>
            <option value="h265">H.265 — до 50% экономии места, воспроизведение не везде</option>
          </select>
        </div>
        <Field label="Битрейт записи (кбит/с)" value={s.record_bitrate} onChange={upd("record_bitrate")} hint="0 — автоматическое качество (CRF); больше 0 — фиксированный потолок, предсказуемый размер архива" />
        <div style={{ marginBottom: 14 }}>
          <label>Только ключевые кадры (I-frame only)</label>
          <select value={s.record_iframe_only ?? "0"} onChange={upd("record_iframe_only")}>
            <option value="0">Выключено — обычное сжатие</option>
            <option value="1">Включено — максимальная экономия места, файлы крупнее при равном качестве</option>
          </select>
        </div>
      </div>

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

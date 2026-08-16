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
  const [profileNote, setProfileNote] = useState("");

  const load = () => api.getSettings().then(setS).catch(() => {});
  useEffect(() => {
    load();
    api.listProfiles().then((r: any) => {
      setTitles(r.titles || {});
      setProfileNote(r.note || "");
    }).catch(() => {});
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
        record_segment_min: parseInt(s.record_segment_min) || 5,
        analytics_cameras_max: parseInt(s.analytics_cameras_max) || 2,
        disk_min_free_pct: parseInt(s.disk_min_free_pct) || 5,
        disk_warn_pct: parseInt(s.disk_warn_pct) || 80,
        disk_crit_pct: parseInt(s.disk_crit_pct) || 90,
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
              style={{ width: "100%", textAlign: "left", whiteSpace: "normal", lineHeight: 1.35 }}
              onClick={() => applyProfile(p)}>
              {titles[p] || p}
            </button>
          </div>
        ))}
        {/* SPEC §19: профиль относится только к слою аналитики. Без этой
            строки администратор объекта на 120 камер разумно предполагает,
            что переключение затронет запись. */}
        {profileNote && (
          <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>{profileNote}</div>
        )}
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
        <Field label="Длительность сегмента записи (мин)" value={s.record_segment_min} onChange={upd("record_segment_min")} hint="5–10 минут. Архив пишется как есть, без перекодирования, поэтому кодек и битрейт задаются на самой камере" />
        <Field label="Камер в режиме аналитики, максимум" value={s.analytics_cameras_max} onChange={upd("analytics_cameras_max")} hint="Запись идёт по всем камерам; распознавание лиц — только на этом числе выбранных, оно и определяет нагрузку на процессор" />
      </div>

      <div className="card" style={{ maxWidth: 520, marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Детекция и хранение</h3>
        <Field label="Глубина хранения архива (дней)" value={s.retention_days} onChange={upd("retention_days")} hint="После — видео и события удаляются автоматически. Отдельным камерам можно задать свою глубину в карточке камеры" />
        <Field label="Порог циклической перезаписи (% свободного)" value={s.disk_min_free_pct} onChange={upd("disk_min_free_pct")} hint="Когда свободного места меньше — удаляются самые старые сегменты, даже если их срок хранения не истёк. Страховка от остановки записи на переполненном диске" />
        <Field label="Предупреждение о заполнении диска (%)" value={s.disk_warn_pct} onChange={upd("disk_warn_pct")} hint="Порог первого предупреждения в мониторинге и логах" />
        <Field label="Критическое заполнение диска (%)" value={s.disk_crit_pct} onChange={upd("disk_crit_pct")} hint="Порог критического алерта" />
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

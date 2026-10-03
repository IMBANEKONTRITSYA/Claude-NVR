import { useEffect, useState } from "react";
import { api, backupDownloadUrl } from "../api";
import { useUI } from "../ui";
import { settingsPayload } from "../settingsPayload";
import { backupHealth, formatBytes, formatStamp } from "../backupView";

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

// SPEC §11 «Резервное копирование: автоматическое раз в сутки + ручной
// запуск»; §18 отводит бэкапам отдельную строку матрицы прав, и страница
// настроек admin-only (App.tsx), то есть строка соблюдается маршрутом, а
// не только проверкой на сервере.
//
// До цикла 47 ручной запуск существовал единственным способом — `docker
// compose exec backup /backup/run.sh`, то есть требовал доступа к
// докер-сокету сервера; в production (.deb + systemd, §26) бэкапа не было
// вовсе. Здесь администратор видит, работает ли автоматический бэкап,
// снимает копию руками и забирает файл.
function Backups() {
  const { toast } = useUI();
  const [state, setState] = useState<any>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api.listBackups().then(setState).catch(() => setState(null));
  useEffect(() => { load(); }, []);

  const create = async () => {
    setBusy(true);
    try {
      const r: any = await api.createBackup();
      toast(`Копия снята: ${r.name} (${formatBytes(r.size_bytes)})`, "ok");
      await load();
    } catch (e: any) { toast(e.message, "err"); }
    finally { setBusy(false); }
  };

  const remove = async (name: string) => {
    if (!confirm(`Удалить резервную копию ${name}? Действие необратимо.`)) return;
    try {
      await api.deleteBackup(name);
      await load();
    } catch (e: any) { toast(e.message, "err"); }
  };

  if (!state) return null;
  const health = backupHealth(state);
  const color = health.level === "ok" ? "var(--green)"
    : health.level === "warn" ? "var(--yellow, #d90)" : "var(--red)";
  const items: any[] = state.items || [];

  return (
    <div className="card" style={{ maxWidth: 520, marginBottom: 16 }}>
      <h3 style={{ marginTop: 0 }}>Резервное копирование</h3>
      <div style={{ color, fontSize: 13, marginBottom: 8 }}>{health.text}</div>
      <div className="muted" style={{ fontSize: 12, marginBottom: 10 }}>
        Каталог: <code>{state.dir}</code><br />
        Расписание: <code>{state.schedule}</code>, хранение {state.retention_days} дн.
        {state.free_bytes !== null && <> · свободно {formatBytes(state.free_bytes)}</>}
        <br />
        Копий: {state.count} на {formatBytes(state.total_bytes)}
      </div>
      <button className="btn" onClick={create} disabled={busy || !state.pg_dump?.ready}>
        {busy ? "Снимаю дамп..." : "Создать копию сейчас"}
      </button>
      {items.length > 0 && (
        <table style={{ width: "100%", marginTop: 12, fontSize: 12 }}>
          <tbody>
            {items.slice(0, 10).map((b: any) => (
              <tr key={b.name}>
                <td>{formatStamp(b.name)}</td>
                <td style={{ textAlign: "right" }}>{formatBytes(b.size_bytes)}</td>
                <td style={{ textAlign: "right", whiteSpace: "nowrap" }}>
                  <a href={backupDownloadUrl(b.name)} download>скачать</a>
                  {" · "}
                  <a href="#" onClick={e => { e.preventDefault(); remove(b.name); }}>удалить</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {items.length > 10 && (
        <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
          Показаны 10 последних из {items.length}; остальные лежат в каталоге выше.
        </div>
      )}
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
      const r = await api.putSettings(settingsPayload(s));
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

  // Кнопка шлёт письмо по СОХРАНЁННЫМ настройкам, а не по тому, что сейчас в
  // форме: проверять несохранённое значило бы «тест прошёл, а алерты не
  // ходят» после ухода со страницы без нажатия «Сохранить».
  const testMail = async () => {
    try {
      const r: any = await api.testEmail();
      toast(`Письмо отправлено: ${(r.recipients || []).join(", ")}`, "ok");
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
        {/* SPEC §15: профиль относится только к слою аналитики. Без этой
            строки администратор большого объекта разумно предполагает,
            что переключение затронет запись всех камер. */}
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
        <Field label="Камер в режиме аналитики, максимум" value={s.analytics_cameras_max} onChange={upd("analytics_cameras_max")} hint="Запись идёт по всем камерам; распознавание лиц — только на этом числе выбранных, оно и определяет нагрузку на процессор. Сколько выдержит именно этот сервер — считает «Автоконфигурация» в разделе «Мониторинг»; поднимать значение выше предложенного стоит ступенями, сверяя фактический FPS по каналам" />
        <Field label="Потоков CPU на камеру аналитики" value={s.analytics_threads} onChange={upd("analytics_threads")} hint="0 — подобрать автоматически: половина ядер сервера делится между камерами аналитики, но не больше 2 потоков на камеру. Больше потоков ускоряет одну камеру и замедляет остальные: замер даёт 9.7 FPS на канал при одном потоке и 4.6 при неограниченном пуле на четырёх камерах. Применяется после перезагрузки модели (~10 с)" />
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

      {/* SPEC §11 «Настройки уведомлений: Telegram, email, звук». Те же
          настройки использует §8 (авто-отправка отчётов по расписанию). */}
      <div className="card" style={{ maxWidth: 520, marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Почтовые уведомления (SMTP)</h3>
        <Field type="text" label="SMTP-сервер" value={s.smtp_host} onChange={upd("smtp_host")} hint="Например, smtp.yandex.ru; оставьте пустым, чтобы отключить почту" />
        <Field label="Порт" value={s.smtp_port} onChange={upd("smtp_port")} hint="587 для STARTTLS, 465 для SSL, 25 для внутреннего релея без шифрования" />
        <div style={{ marginBottom: 14 }}>
          <label>Шифрование</label>
          <select value={s.smtp_tls || "starttls"} onChange={upd("smtp_tls")}>
            <option value="starttls">STARTTLS (порт 587)</option>
            <option value="ssl">SSL/TLS (порт 465)</option>
            <option value="none">Без шифрования (внутренняя сеть)</option>
          </select>
        </div>
        <Field type="text" label="Логин" value={s.smtp_user} onChange={upd("smtp_user")} hint="Оставьте пустым для релея без авторизации" />
        <Field type="password" label="Пароль" value={s.smtp_password} onChange={upd("smtp_password")} hint="Хранится в БД в зашифрованном виде" />
        <Field type="text" label="Отправитель (From)" value={s.smtp_from} onChange={upd("smtp_from")} hint="Пусто — будет использован логин" />
        <Field type="text" label="Получатели алертов" value={s.alert_email_to} onChange={upd("alert_email_to")} hint="Через запятую. Сюда приходят алерты watchlist и переполнения диска" />
        <button className="btn secondary" onClick={testMail}>Отправить тестовое письмо</button>
      </div>

      <div className="card" style={{ maxWidth: 520, marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Звуковое оповещение</h3>
        <div style={{ marginBottom: 14 }}>
          <label>
            <input type="checkbox" style={{ width: "auto", marginRight: 8 }}
                   checked={String(s.alert_sound_enabled) === "1"}
                   onChange={e => setS((p: any) => ({ ...p, alert_sound_enabled: e.target.checked ? 1 : 0 }))} />
            Звук при обнаружении персоны из watchlist
          </label>
          <div className="muted" style={{ fontSize: 11, marginTop: 2 }}>
            Сигнал подаётся на открытой вкладке «Стена распознавания» (SPEC §6)
          </div>
        </div>
      </div>

      <Backups />

      <div style={{ maxWidth: 520 }}>
        {msg && <div style={{ marginBottom: 10, color: msg.type === "ok" ? "var(--green)" : "var(--red)" }}>{msg.text}</div>}
        <button className="btn" onClick={save} disabled={busy}>{busy ? "Сохранение..." : "Сохранить"}</button>
      </div>
    </div>
  );
}

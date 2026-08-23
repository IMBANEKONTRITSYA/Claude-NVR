import { useEffect, useState } from "react";
import { api, getToken, getRole } from "../api";
import {
  formatBitrate, formatFps, formatTotalBitrate, isSilentStream,
} from "../streamRate";
import { AnalyticsSource, streamCell } from "../analyticsStream";
import { modelBanner } from "../modelLoad";
import { cleanupBanner } from "../archiveCleanup";

function Bar({ percent, warn, crit }: { percent: number; warn?: number; crit?: number }) {
  // Умолчания через ?? , а не в сигнатуре: вызывающие передают warn/crit
  // из настроек, и явный undefined (настройка ещё не загрузилась) должен
  // падать на умолчание, а не красить полосу по NaN.
  const w = warn ?? 75;
  const c = crit ?? 90;
  const color = percent >= c ? "var(--red)" : percent >= w ? "var(--orange)" : "var(--green)";
  return (
    <div style={{ background: "var(--bg)", borderRadius: 4, height: 8, overflow: "hidden", marginTop: 4 }}>
      <div style={{ width: `${Math.min(100, percent)}%`, height: "100%", background: color, transition: "width .3s" }} />
    </div>
  );
}

function Metric({ label, value, percent, hint, warn, crit }: any) {
  return (
    <div className="card" style={{ flex: 1, minWidth: 200 }}>
      <div className="muted">{label}</div>
      <div className="kpi" style={{ fontSize: 24 }}>{value}</div>
      {/* Пороги пробрасываются в полосу: у диска архива они настраиваемые
          (SPEC §14), и полоса обязана краснеть на том же значении, на
          котором приходит алерт, а не на умолчании компонента. */}
      {percent !== undefined && <Bar percent={percent} warn={warn} crit={crit} />}
      {hint && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

/** Слой записи: статус каждого из 120 потоков (SPEC §14, §9). */
function RecordLayerPanel() {
  const [d, setD] = useState<any>(null);
  const [err, setErr] = useState("");
  // На 120 камерах таблица целиком нечитаема, а интересны почти всегда
  // проблемные потоки — поэтому фильтр, и по умолчанию он на них.
  const [filter, setFilter] = useState<"problems" | "all">("problems");

  useEffect(() => {
    const tick = () => api.recordLayer().then(r => { setD(r); setErr(""); }).catch(e => setErr(e.message));
    tick();
    const t = setInterval(tick, 10000);
    return () => clearInterval(t);
  }, []);

  if (err) return <div className="card" style={{ marginBottom: 16 }}><h3 style={{ marginTop: 0 }}>Слой записи</h3><div className="empty">{err}</div></div>;
  if (!d) return null;

  const s = d.summary;
  const gaps: number[] = d.segment_gaps || [];
  const streams: any[] = d.streams || [];
  const problems = streams.filter(x => x.status !== "online" || gaps.includes(x.camera_id));
  const shown = filter === "problems" ? problems : streams;

  const color = (st: string) =>
    st === "online" ? "var(--green)" : st === "offline" ? "var(--red)" : "var(--orange)";
  const label = (st: string) =>
    st === "online" ? "пишется" : st === "offline" ? "потерян" : "неизвестно";

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h3 style={{ marginTop: 0 }}>Слой записи</h3>

      {/* Недоступный Control API — причина, по которой статусы всех
          потоков «неизвестны». Без неё интерфейс показывал бы «неизвестно»
          на всей стене камер без единого намёка, куда смотреть. */}
      {d.control_api_error && (
        <div style={{
          padding: "8px 12px", borderRadius: 4, marginBottom: 12,
          background: "var(--red)", color: "#fff", fontWeight: 600,
        }}>
          Нет связи с медиасервером MediaMTX — статусы потоков записи неизвестны,
          пути записи не синхронизируются.
          <div style={{ fontWeight: 400, fontSize: 12, marginTop: 4 }}>
            {d.control_api_error}
          </div>
        </div>
      )}

      {/* Корни медиаданных разведены: MediaMTX пишет сегменты в один
          каталог, а архив индексирует другой. Симптом — «пропуск записи»
          разом на всех камерах и диск, который retention не чистит;
          догадаться по нему о причине нельзя, поэтому она показывается
          явно (SPEC §5 «путь архива конфигурируется под отдельный диск»). */}
      {d.record_root_warning && (
        <div style={{
          padding: "8px 12px", borderRadius: 4, marginBottom: 12,
          background: "var(--orange)", color: "#000", fontWeight: 600,
        }}>
          Каталог записи MediaMTX и каталог архива заданы по-разному.
          <div style={{ fontWeight: 400, fontSize: 12, marginTop: 4 }}>
            {d.record_root_warning}
          </div>
        </div>
      )}

      {/* Отказ аналитики показывается отдельно от записи: с цикла 26 он
          больше не роняет воркер (SPEC §2), поэтому без явного сообщения
          «распознавание молчит» неотличимо от «в кадре никого нет».
          С цикла 55 у состояния три исхода, а не два: загрузка идёт в
          своей нити и «ещё грузится» стало обычным и длительным — авария
          в этом случае звала бы дежурного чинить то, что качается. Выбор
          вынесен в modelLoad.ts и проверяется юнит-тестами. */}
      {(() => {
        const b = modelBanner(d.analytics);
        if (!b) return null;
        return (
          <div style={{
            padding: "8px 12px", borderRadius: 4, marginBottom: 12,
            background: b.tone === "warn" ? "var(--orange)" : "var(--panel)",
            color: b.tone === "warn" ? "#000" : "inherit",
            border: b.tone === "warn" ? "none" : "1px solid var(--border)",
            fontWeight: 600,
          }}>
            {b.title}
            {b.detail && (
              <div style={{ fontWeight: 400, fontSize: 12, marginTop: 4 }}>
                {b.detail}
              </div>
            )}
            {b.hint && (
              <div style={{ fontWeight: 400, fontSize: 12, marginTop: 4 }}>
                {b.hint}
              </div>
            )}
          </div>
        );
      })()}

      {/* Часовая уборка архива (§5). С цикла 57 она идёт в своей нити
          воркера и сторожем живости не проверяется — плата за то, что
          долгий проход больше не останавливает статусы потоков и
          циклическую перезапись (SPEC §2). Поэтому её состояние
          показывается здесь, и только когда от дежурного что-то зависит:
          проход упал, проходы не укладываются в час, проход затянулся.
          Выбор вынесен в archiveCleanup.ts и проверяется юнит-тестами. */}
      {(() => {
        const b = cleanupBanner(d.cleanup);
        if (!b) return null;
        return (
          <div style={{
            padding: "8px 12px", borderRadius: 4, marginBottom: 12,
            background: b.tone === "warn" ? "var(--orange)" : "var(--panel)",
            color: b.tone === "warn" ? "#000" : "inherit",
            border: b.tone === "warn" ? "none" : "1px solid var(--border)",
            fontWeight: 600,
          }}>
            {b.title}
            {b.detail && (
              <div style={{ fontWeight: 400, fontSize: 12, marginTop: 4 }}>
                {b.detail}
              </div>
            )}
          </div>
        );
      })()}

      {!d.available ? (
        <div className="empty">
          {d.reason || "Нет данных"} — за сутки записано {d.segments_last_day} сегментов
          ({d.gb_last_day} ГБ), камер включено {d.cameras_enabled}.
        </div>
      ) : (
        <>
          <div className="row">
            <Metric label="Потоков пишется" value={`${s.streams_online} / ${s.streams_total}`}
              percent={s.streams_total ? (s.streams_online * 100) / s.streams_total : 0}
              warn={101} crit={102}
              hint={s.streams_unknown
                ? `${s.streams_unknown} — состояние неизвестно (нет связи с медиасервером)`
                : `потеряно ${s.streams_offline}`} />
            <Metric label="Сегментов за сутки" value={d.segments_last_day}
              hint={`${d.gb_last_day} ГБ записано`} />
            <Metric label="Пропусков записи" value={gaps.length}
              hint={gaps.length ? `камеры: ${gaps.join(", ")}` : "нет"} />
            <Metric label="Кадров с ошибками" value={s.frames_in_error}
              hint="суммарно по всем потокам" />
            {/* Суммарный входящий поток (SPEC §9 «битрейт», §16 «Сеть:
                сумма битрейтов всех камер + 10% запас»). Подпись говорит,
                по скольким камерам он сложен: сразу после старта воркера
                измерены ещё не все, и сумма без этой оговорки выглядела бы
                заниженной нагрузкой на канал — тем самым числом, по
                которому канал и планируют. */}
            <Metric label="Входящий поток" value={formatTotalBitrate(s.inbound_kbps)}
              hint={s.inbound_kbps === null || s.inbound_kbps === undefined
                ? "ещё не измерен"
                : `измерено камер: ${s.bitrate_measured_cameras} из ${s.streams_total}`} />
          </div>

          {gaps.length > 0 && (
            <div style={{
              padding: "8px 12px", borderRadius: 4, marginTop: 12,
              background: "var(--red)", color: "#fff", fontWeight: 600,
            }}>
              Пропуск записи сегментов на камерах: {gaps.join(", ")} — поток есть,
              но файлы не пишутся. Проверьте место на диске и права на каталог архива.
            </div>
          )}

          <div style={{ marginTop: 12, marginBottom: 8 }}>
            <button className={filter === "problems" ? "btn" : "btn secondary"}
              onClick={() => setFilter("problems")}>
              Проблемные ({problems.length})
            </button>
            <button className={filter === "all" ? "btn" : "btn secondary"}
              style={{ marginLeft: 8 }} onClick={() => setFilter("all")}>
              Все потоки ({streams.length})
            </button>
          </div>

          {shown.length === 0 ? (
            <div className="empty">
              {filter === "problems" ? "Все потоки пишутся нормально" : "Потоков нет"}
            </div>
          ) : (
            <div style={{ maxHeight: 320, overflowY: "auto" }}>
              <table>
                <thead>
                  {/* FPS и битрейт §9 стоят сразу за статусом: раздел
                      называет все три величины одной строкой, и читаются
                      они тоже вместе — «онлайн, 25 к/с, 4.2 Мбит/с».
                      «Принято» остаётся, но уже как справка об объёме, а
                      не как подмена скорости. */}
                  <tr><th>Камера</th><th>Статус</th><th>FPS</th><th>Битрейт</th>
                    <th>Принято</th><th>Ошибки кадров</th><th>В сети с</th></tr>
                </thead>
                <tbody>
                  {shown.map(x => (
                    <tr key={x.camera_id}>
                      <td>#{x.camera_id} {x.name}</td>
                      <td style={{ color: color(x.status), fontWeight: 600 }}>
                        {label(x.status)}
                        {gaps.includes(x.camera_id) && " · пропуск сегмента"}
                        {/* Восстановление после обрыва (SPEC §19): воркер
                            опрашивает камеру и пересоздаёт путь, как только
                            она отвечает. Без этой строки «камера выключена»
                            и «камера отвечает, а запись не идёт» выглядят
                            здесь одинаково, а чинятся по-разному: первое —
                            на объекте, второе — в настройках потока. */}
                        {x.recovery && (
                          <div className="muted" style={{ fontWeight: 400, fontSize: 12 }}>
                            {x.recovery.camera_answering
                              ? "камера отвечает по RTSP, восстанавливаю запись"
                              : "камера не отвечает по RTSP"}
                            {" · "}обрыв {Math.round(x.recovery.down_for_sec)} с
                            {x.recovery.kicks > 0 && ` · попыток ${x.recovery.kicks}`}
                          </div>
                        )}
                      </td>
                      <td>{formatFps(x.fps)}</td>
                      {/* Поток числится живым, а байты не идут — состояние,
                          которого накопительный счётчик не показывал вовсе.
                          Красится только оно: прочерк «ещё не измерено»
                          аварией не является. */}
                      <td style={{
                        color: isSilentStream(x.status, x.bitrate_kbps)
                          ? "var(--red)" : undefined,
                        fontWeight: isSilentStream(x.status, x.bitrate_kbps) ? 600 : undefined,
                      }}>
                        {formatBitrate(x.bitrate_kbps)}
                        {isSilentStream(x.status, x.bitrate_kbps) && " · данных нет"}
                      </td>
                      <td className="muted">{(x.inbound_bytes / 1048576).toFixed(1)} МБ</td>
                      <td style={{ color: x.frames_in_error ? "var(--orange)" : undefined }}>
                        {x.frames_in_error}
                      </td>
                      <td className="muted">
                        {x.online_since ? new Date(x.online_since).toLocaleString("ru-RU") : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/**
 * Автоконфигурация при первом запуске (SPEC §16).
 *
 * Отличие от калькулятора хранения ниже: тот отвечает на вопрос «сколько
 * диска под столько-то камер», то есть требует, чтобы администратор уже
 * знал число камер. Этот отвечает на обратный и более ранний вопрос —
 * «сколько камер потянет вот этот сервер», — который встаёт до того, как
 * заведена первая камера, и на который §16 требует ответа от системы.
 *
 * Предел по каждому ресурсу показывается рядом с итогом намеренно: «132
 * камеры» без «упирается в диск» не подсказывает, что менять, чтобы стало
 * больше. Именно за этим администратор и открывает экран планирования.
 */
function AutoConfigPanel() {
  const [f, setF] = useState({ bitrate_kbps: 2048, retention_days: 14 });
  const [d, setD] = useState<any>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  const load = (bitrate = f.bitrate_kbps, days = f.retention_days) =>
    api.autoconfig(bitrate, days)
      .then((r: any) => { setD(r); setErr(""); })
      .catch((e: any) => { setD(null); setErr(e.message); });

  useEffect(() => { load(); }, []);

  const apply = async () => {
    setBusy(true); setMsg(""); setErr("");
    try {
      const r = await api.autoconfigApply(f.bitrate_kbps, f.retention_days);
      setMsg(`Применено: камер аналитики не более ${r.applied.analytics_cameras_max}, `
        + `профиль «${r.applied.performance_profile}». `
        + "Воркер подхватит настройки за ~10 секунд.");
      await load();
    } catch (e: any) { setErr(e.message); }
    finally { setBusy(false); }
  };

  const res = d?.resources, plan = d?.plan;
  const RU: Record<string, string> = { cpu: "CPU", ram: "оперативную память", disk: "диск" };

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h3 style={{ marginTop: 0 }}>
        Автоконфигурация{" "}
        {d?.first_run && <span className="badge" style={{ background: "#2d6" }}>первый запуск</span>}
      </h3>
      <p className="muted" style={{ marginTop: 0 }}>
        Система определяет ресурсы сервера и предлагает пределы (ТЗ §16). Расход на камеру
        берётся по верхней границе вилок ТЗ, из ресурсов вычитается запас 20% (§19) и слой
        приложения — предложение сознательно осторожнее, чем «влезет впритык».
      </p>

      <div className="grid" style={{ gridTemplateColumns: "repeat(2, 1fr)", gap: 8 }}>
        <div>
          <label>Битрейт основного потока, кбит/с</label>
          <input type="number" min={64} max={100000} value={f.bitrate_kbps}
            onChange={e => setF({ ...f, bitrate_kbps: Number(e.target.value) })} />
        </div>
        <div>
          <label>Глубина хранения, суток</label>
          <input type="number" min={1} max={3650} value={f.retention_days}
            onChange={e => setF({ ...f, retention_days: Number(e.target.value) })} />
        </div>
      </div>
      <button className="btn" style={{ marginTop: 8 }} onClick={() => load()}>Пересчитать</button>

      {err && <div className="empty" style={{ marginTop: 8 }}>{err}</div>}
      {msg && <div className="muted" style={{ marginTop: 8, color: "#2d6" }}>{msg}</div>}

      {res && plan && (
        <>
          <div className="muted" style={{ marginTop: 12, fontSize: 12 }}>
            Обнаружено: {res.cores_physical} физ. ядер ({res.cores_logical} потоков),{" "}
            {Math.round(res.ram_mb / 1024)} ГБ ОЗУ, свободно на диске архива{" "}
            {res.disk_free_gb} ГБ из {res.disk_total_gb} ГБ,{" "}
            {res.gpu ? "обнаружен ускоритель" : "ускоритель не обнаружен"}.
          </div>

          <div className="row" style={{ marginTop: 12 }}>
            <Metric label="Камер записи" value={plan.recording_max}
              hint={`упирается в ${RU[plan.recording.bound_by]}; по CPU ${plan.recording.by_cpu}, `
                + `по ОЗУ ${plan.recording.by_ram}, по диску ${plan.recording.by_disk}`} />
            <Metric label="Камер аналитики" value={plan.analytics_max}
              hint={`по ресурсам ${plan.analytics_by_resources}, `
                + `упирается в ${RU[plan.analytics.bound_by]}`} />
            <Metric label="Профиль" value={plan.profile}
              hint={`свободно ${plan.budget.cores_left_for_analytics} ядра под аналитику`} />
          </div>

          {(plan.warnings || []).map((w: string, i: number) => (
            <div key={i} className="empty" style={{ marginTop: 8 }}>{w}</div>
          ))}

          <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>
            Сейчас настроено: камер заведено {d.current.cameras_total} (в аналитике{" "}
            {d.current.cameras_analytics}), предел аналитики{" "}
            {d.current.analytics_cameras_max}, профиль{" "}
            {d.current.performance_profile || "не задан"}.
            {d.applied_at && ` Автоконфигурация применялась ${d.applied_at}.`}
          </div>

          <button className="btn" style={{ marginTop: 8 }} onClick={apply} disabled={busy}>
            {busy ? "Применение..." : "Применить предложение"}
          </button>
          <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>
            Применяются только предел камер аналитики и профиль. Глубина хранения и уже
            заведённые камеры не трогаются.
          </div>
        </>
      )}
    </div>
  );
}

/** Калькулятор хранения (SPEC §16): битрейт × камеры × дни → требуемый объём.
 *
 * Поля «Камер» и «Глубина хранения» при открытии пусты не по недосмотру:
 * сервер подставляет фактическое число включённых камер и настроенный
 * retention, и первый ответ относится к ЭТОЙ системе. Раньше в форме
 * стояло `cameras: 120`, и администратор объекта на 32 камеры при каждом
 * открытии видел расчёт для 120 — §22 прямо запрещает хардкодить число
 * камер, а §16 требует «формулы и калькуляторы вместо фиксированных чисел».
 *
 * Как только человек введёт своё число, оно и уходит на сервер — сценарий
 * «что если камер станет вдвое больше» работает как работал.
 */
function StorageCalculator() {
  // undefined ≠ 0: «не задано, возьми фактическое» против «ноль камер».
  const [f, setF] = useState<{ bitrate_kbps: number; cameras?: number; days?: number }>(
    { bitrate_kbps: 2048, cameras: undefined, days: undefined });
  const [res, setRes] = useState<any>(null);
  const [err, setErr] = useState("");

  const calc = (over?: { cameras?: number; days?: number }) =>
    api.storageCalc(f.bitrate_kbps, over?.cameras ?? f.cameras, over?.days ?? f.days)
      .then(r => {
        setRes(r); setErr("");
        // Подставленные сервером значения показываем в полях: иначе форма
        // пуста, а результат посчитан — непонятно, для чего именно.
        setF(prev => ({
          ...prev,
          cameras: prev.cameras ?? r.cameras,
          days: prev.days ?? r.days,
        }));
      })
      .catch(e => { setRes(null); setErr(e.message); });

  useEffect(() => { calc(); }, []);

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h3 style={{ marginTop: 0 }}>Калькулятор хранения</h3>
      <p className="muted" style={{ marginTop: 0 }}>
        Сколько диска нужно под заданную глубину архива. Формула ТЗ: Мбит/с × 10.8 = ГБ/сутки на камеру.
      </p>
      <div className="grid" style={{ gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
        <div>
          <label>Битрейт основного потока, кбит/с</label>
          <input type="number" min={64} max={100000} value={f.bitrate_kbps}
            onChange={e => setF({ ...f, bitrate_kbps: Number(e.target.value) })} />
        </div>
        <div>
          <label>Камер</label>
          <input type="number" min={1} max={1000} value={f.cameras ?? ""}
            placeholder="сейчас в системе"
            onChange={e => setF({ ...f, cameras: e.target.value === "" ? undefined : Number(e.target.value) })} />
        </div>
        <div>
          <label>Глубина хранения, суток</label>
          <input type="number" min={1} max={3650} value={f.days ?? ""}
            placeholder="из настроек"
            onChange={e => setF({ ...f, days: e.target.value === "" ? undefined : Number(e.target.value) })} />
        </div>
      </div>
      {res?.cameras_source === "fallback_empty" && (
        <div className="muted" style={{ marginTop: 8 }}>
          Камеры ещё не заведены — расчёт показан для одной. Введите
          планируемое число.
        </div>
      )}
      <button className="btn" style={{ marginTop: 8 }} onClick={() => calc()}>Рассчитать</button>
      {err && <div className="empty" style={{ marginTop: 8 }}>{err}</div>}
      {res && (
        <div className="row" style={{ marginTop: 12 }}>
          <Metric label="На камеру" value={`${res.gb_per_day_per_camera} ГБ/сут`} />
          <Metric label="Суммарно" value={`${res.gb_per_day_total} ГБ/сут`} />
          <Metric label="Требуется" value={`${res.required_tb} ТБ`}
            hint={`${res.required_gb} ГБ на ${res.days} сут`} />
        </div>
      )}
    </div>
  );
}

/** Хранилище архива: заполнение, фактический расход, прогноз (SPEC §5, §21). */
function StoragePanel({ isAdmin }: { isAdmin: boolean }) {
  const [s, setS] = useState<any>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    const tick = () => api.storage().then(r => { setS(r); setErr(""); }).catch(e => setErr(e.message));
    tick();
    const t = setInterval(tick, 15000);
    return () => clearInterval(t);
  }, []);

  if (err) return <div className="card" style={{ marginBottom: 16 }}><h3 style={{ marginTop: 0 }}>Хранилище</h3><div className="empty">{err}</div></div>;
  if (!s) return null;

  const overrides = Object.entries(s.per_camera_retention || {});
  // Прочерк вместо числа, пока расход не измерен: см. days_left() в
  // services/storage.py — «бесконечность» в первые минуты была бы враньём.
  const left = s.days_left == null ? "—" : `${s.days_left} сут`;
  const alertText = s.alert_level === "critical"
    ? `Диск заполнен более чем на ${s.crit_percent}% — старейшие сегменты будут удалены автоматически`
    : s.alert_level === "warning"
      ? `Диск заполнен более чем на ${s.warn_percent}%`
      : "";

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <h3 style={{ marginTop: 0 }}>Хранилище архива</h3>
      {alertText && (
        <div style={{
          padding: "8px 12px", borderRadius: 4, marginBottom: 12,
          background: s.alert_level === "critical" ? "var(--red)" : "var(--orange)",
          color: "#fff", fontWeight: 600,
        }}>{alertText}</div>
      )}
      <div className="row">
        <Metric label="Заполнение диска" value={`${s.disk_used_percent}%`}
          percent={s.disk_used_percent} warn={s.warn_percent} crit={s.crit_percent}
          hint={`свободно ${s.disk_free_gb} ГБ из ${s.disk_total_gb} ГБ`} />
        <Metric label="Хватит места на" value={left}
          hint={s.forecast_source === "measured"
            ? `по фактическому расходу ${s.measured_gb_per_day} ГБ/сут`
            : `расчётно, ${s.nominal_gb_per_day} ГБ/сут — фактических данных пока нет`} />
        <Metric label="Расход за сутки" value={`${s.measured_gb_per_day} ГБ`}
          hint={`расчётный ${s.nominal_gb_per_day} ГБ · сегментов ${s.segments_last_day}`} />
        <Metric label="Объём архива" value={`${s.archive_gb} ГБ`}
          hint={`глубина хранения ${s.retention_days} сут · камер на записи ${s.cameras_recording}`} />
      </div>
      {s.calibration != null && (
        <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
          Калибровка: фактический расход составляет {Math.round(s.calibration * 100)}% от расчётного
          {s.calibration < 1 ? " (VBR и smart-кодек экономят)" : " (выше расчёта — проверьте битрейт камер)"}.
        </div>
      )}
      {overrides.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>
            Камеры с собственной глубиной хранения:
          </div>
          <div>{overrides.map(([id, d]: any) => (
            <span key={id} className="badge" style={{ marginRight: 6 }}>#{id}: {d} сут</span>
          ))}</div>
        </div>
      )}
      {isAdmin && (
        <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
          Глобальная глубина хранения и пороги алертов настраиваются в разделе «Настройки».
        </div>
      )}
    </div>
  );
}

/** Ячейка «Поток аналитики»: подпись, цвет и пояснение от воркера. */
function AnalyticsStreamCell({ src }: { src: AnalyticsSource }) {
  const cell = streamCell(src);
  const color = cell.tone === "warn" ? "var(--orange)"
    : cell.tone === "muted" ? "var(--muted)" : undefined;
  return (
    <span title={cell.note}>
      <span style={{ color, fontWeight: cell.tone === "warn" ? 600 : undefined }}>
        {cell.label}
      </span>
      {/* Пояснение стоит под подписью, а не только в title: причина, по
          которой аналитика уехала с субпотока, — это то, ради чего строка
          вообще показывается, и прятать её в подсказку значит не показать
          вовсе (§9 требует статус потоков, а не намёк на него). */}
      {cell.tone === "warn" && cell.note && (
        <div className="muted" style={{ fontSize: 11 }}>{cell.note}</div>
      )}
    </span>
  );
}

export function Monitoring() {
  const [m, setM] = useState<any>(null);
  const [err, setErr] = useState("");
  const isAdmin = getRole() === "admin";

  useEffect(() => {
    const tick = () => api.sysMetrics().then(r => { setM(r); setErr(""); }).catch(e => setErr(e.message));
    tick();
    const t = setInterval(tick, 5000);
    return () => clearInterval(t);
  }, []);

  if (err) return <div><h2>Мониторинг</h2><div className="empty">{err}</div></div>;
  if (!m) return <div><h2>Мониторинг</h2><div className="empty">Загрузка...</div></div>;

  // Строки таблицы аналитики — объединение двух источников, а не только
  // camera_fps: камера, у которой поток аналитики выбран, но детекция ещё
  // не намерила первое окно FPS, обязана быть видна. Обратный случай тоже
  // возможен (воркер прежней версии не публикует источник), и тогда в
  // столбце потока просто прочерк.
  const analyticsSource: Record<string, any> = m.camera_analytics_source || {};
  const camIds = Array.from(
    new Set([...Object.keys(m.camera_fps || {}), ...Object.keys(analyticsSource)])
  ).sort((a, b) => Number(a) - Number(b));

  return (
    <div>
      <h2>Мониторинг системы</h2>

      <div className="row" style={{ marginBottom: 16 }}>
        {/* §18 «Системный мониторинг: Оператор — Ограниченно». Телеметрия
            железа сервера приходит только админу, и признак приходит от
            бэкенда (`system_metrics_limited`), а не выводится из роли на
            клиенте: право решает сервер, клиент лишь объясняет пустоту.
            Без объяснения оператор видел бы страницу без трёх плиток и
            читал бы это как поломку мониторинга. */}
        {!m.system_metrics_limited && <>
          <Metric label="Загрузка CPU" value={`${m.cpu_percent}%`} percent={m.cpu_percent} />
          <Metric label="Оперативная память" value={`${m.ram_percent}%`} percent={m.ram_percent}
            hint={`${m.ram_used_mb} / ${m.ram_total_mb} МБ`} />
        </>}
        <Metric label="Диск архива" value={`${m.disk_percent}%`} percent={m.disk_percent}
          hint={`свободно ${m.disk_free_gb} ГБ из ${m.disk_total_gb} ГБ`} />
        {/* Источник показания подписан под числом: датчиков на сервере
            несколько (сокеты, корпус, диски массива), и «Температура» без
            уточнения читается как температура процессора — а до цикла 52
            ею запросто оказывалась температура корпуса или диска. */}
        {!m.system_metrics_limited && m.temperature_c != null && (
          <Metric label="Температура" value={`${m.temperature_c}°C`}
            hint={m.temperature_source || undefined} />
        )}
      </div>

      {m.system_metrics_limited && (
        <div style={{
          padding: "8px 12px", borderRadius: 4, marginBottom: 16,
          background: "var(--panel)", border: "1px solid var(--border)",
          fontSize: 13,
        }}>
          Загрузка CPU, память и температура сервера доступны только
          администратору (матрица прав, §18). Состояние камер, потоков и
          архива — ниже.
        </div>
      )}

      <RecordLayerPanel />
      <StoragePanel isAdmin={isAdmin} />
      {/* Планирование (§16) стоит перед калькулятором хранения: сначала
          «сколько камер потянет сервер», потом «сколько диска под них». */}
      {isAdmin && <AutoConfigPanel />}
      {isAdmin && <StorageCalculator />}

      <div className="row" style={{ marginBottom: 16 }}>
        <Metric label="Камеры в сети" value={`${m.cameras_online} / ${m.cameras_enabled}`}
          hint={`всего заведено: ${m.cameras_total}`} />
        <Metric label="Событий сегодня" value={m.events_today} />
        <Metric label="Сегментов в архиве" value={m.segments_total} />
        <Metric label="Очередь апскейла" value={m.upscale_queue}
          hint={m.redis_ok ? "Redis доступен" : "Redis недоступен"} />
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <h3 style={{ marginTop: 0 }}>Аналитика по камерам</h3>
        {camIds.length === 0 && <div className="empty">Нет данных — воркер ещё не публиковал метрики</div>}
        {camIds.length > 0 && (
          <table>
            <thead><tr><th>Камера</th><th>FPS детекции</th><th>Поток аналитики</th></tr></thead>
            <tbody>
              {camIds.map((id: string) => {
                const v = (m.camera_fps || {})[id];
                const src = analyticsSource[id];
                return (
                  <tr key={id}>
                    <td>#{id}</td>
                    <td style={{ color: v < 1 ? "var(--orange)" : "var(--green)", fontWeight: 600 }}>
                      {v == null ? "—" : v}
                    </td>
                    <td>{src ? <AnalyticsStreamCell src={src} /> : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {isAdmin && (
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Prometheus</h3>
          <p className="muted">Метрики в формате Prometheus для внешнего мониторинга (Grafana).</p>
          <a className="btn secondary" href={`/api/system/prometheus?token=${getToken()}`} target="_blank" rel="noreferrer">
            Открыть /api/system/prometheus
          </a>
        </div>
      )}
    </div>
  );
}

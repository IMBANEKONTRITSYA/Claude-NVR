import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, getToken, segmentThumbUrl } from "../api";
import {
  DEFAULT_RATE,
  FALLBACK_FRAME_DURATION,
  PLAYBACK_RATES,
  PlaybackRate,
  frameDurationFrom,
  positionLabel,
  stepTime,
} from "../framePlayer";
import {
  Position,
  TimelineRange,
  TimelineSegment,
  clockLabel,
  coverageBars,
  dayWindow,
  formatArchiveTime,
  fractionToTime,
  locateAt,
  nextIndex,
  parseArchiveTime,
  positionToClock,
  recordedLabel,
  timeToFraction,
} from "../archiveTimeline";

/** Миниатюра кадра сегмента (ТЗ §7).
 *
 * Битый или пустой сегмент — штатное состояние архива (обрыв RTSP на
 * первой секунде файла), эндпоинт отвечает на такой 404. Показываем
 * прочерк вместо иконки сломанной картинки браузера: строка остаётся
 * читаемой, а оператор видит, что кадра нет, а не что «сломался архив».
 *
 * `loading="lazy"` обязателен: выдача — до 200 строк, и без него браузер
 * запросил бы все миниатюры разом, а каждая непрогретая — это вызов
 * ffmpeg на сервере.
 */
function SegThumb({ id }: { id: number }) {
  const [failed, setFailed] = useState(false);
  if (failed) return <span className="empty">—</span>;
  return (
    <img
      src={segmentThumbUrl(id)}
      loading="lazy"
      alt=""
      className="seg-thumb"
      onError={() => setFailed(true)}
    />
  );
}

/** Плеер архива: покадровый просмотр и непрерывный ход через файлы (ТЗ §5).
 *
 * Длительность кадра измеряется по самому потоку через
 * `requestVideoFrameCallback`: у камер §1 основной поток идёт 15–30 fps,
 * это настройка камеры, и в `video_segments` её нет. Константа вместо
 * замера означала бы, что на половине камер шаг перепрыгивает кадр или
 * топчется на месте.
 *
 * **Цепочка, а не файл.** Запись §5 непрерывная, а сегмент — пять минут;
 * плеер, играющий один файл, останавливался на границе, и событие,
 * растянутое на два сегмента, оператор досматривал вторым кликом. Здесь
 * конец файла — не конец просмотра: `onAdvance` переводит плеер на
 * следующее звено, и воспроизведение продолжается. Перерыв записи цепочку
 * не рвёт (см. `nextIndex`) — он перескакивается, а видно его на шкале.
 */
function ChainPlayer({ segments, index, seekOffset, onAdvance, onClock }: {
  segments: TimelineSegment[];
  index: number;
  /** Куда встать внутри текущего файла, с. Меняется при клике по шкале. */
  seekOffset: number;
  onAdvance: (next: number) => void;
  onClock: (ms: number) => void;
}) {
  const video = useRef<HTMLVideoElement | null>(null);
  const frameDur = useRef<number>(FALLBACK_FRAME_DURATION);
  const lastFrameTime = useRef<number | null>(null);
  // Точная граница начала показанного кадра. `currentTime` для этого не
  // годится: он отдаёт позицию воспроизведения где-то внутри кадра.
  const anchor = useRef<number | null>(null);
  // Играл ли плеер до смены файла. Без этого переход через границу
  // сегмента останавливал бы просмотр — то есть ровно то, что цепочка и
  // должна была убрать.
  const wasPlaying = useRef(false);
  const [rate, setRate] = useState<PlaybackRate>(DEFAULT_RATE);
  const [pos, setPos] = useState(0);

  const seg = segments[index];
  const src = seg ? `/api/archive/file/${seg.id}?token=${getToken()}` : "";

  // `mediaTime` показанного кадра — точная граница его начала, известная
  // от декодера. На ней стоит весь шаг: длительность кадра измеряется с
  // погрешностью, и отсчёт от неё копил бы ошибку, а якорь
  // переустанавливается на каждом показанном кадре (в том числе после
  // перемотки) и потому не копит ничего.
  useEffect(() => {
    const el = video.current as any;
    if (!el?.requestVideoFrameCallback) return;
    let cancelled = false;
    const onFrame = (_now: number, meta: { mediaTime: number }) => {
      if (cancelled) return;
      const prev = lastFrameTime.current;
      if (prev !== null) {
        // Замер годен только между соседними кадрами воспроизведения;
        // разница через скачок перемотки отбраковывается фильтром.
        const measured = frameDurationFrom(meta.mediaTime - prev);
        if (measured !== null) frameDur.current = measured;
      }
      lastFrameTime.current = meta.mediaTime;
      anchor.current = meta.mediaTime;
      setPos(meta.mediaTime);
      el.requestVideoFrameCallback(onFrame);
    };
    el.requestVideoFrameCallback(onFrame);
    return () => { cancelled = true; };
  }, [src]);

  // Смена источника обнуляет накопленное: кадр предыдущего сегмента к
  // новому отношения не имеет.
  useEffect(() => {
    frameDur.current = FALLBACK_FRAME_DURATION;
    lastFrameTime.current = null;
    anchor.current = null;
    setPos(0);
  }, [src]);

  // Встать в запрошенную секунду нового файла и, если до перехода играли,
  // продолжить играть.
  //
  // Перемотка делается по `loadedmetadata`, а не сразу после смены `src`:
  // до загрузки метаданных `duration` неизвестна, и присвоение
  // `currentTime` браузер молча отбрасывает — клик по шкале попадал бы в
  // начало файла вместо запрошенной минуты. `readyState` проверяется
  // отдельно на случай, когда метаданные уже загружены и события не будет
  // (повторный клик по тому же сегменту).
  useEffect(() => {
    const el = video.current;
    if (!el) return;
    const apply = () => {
      if (seekOffset > 0 && Number.isFinite(el.duration)) {
        el.currentTime = Math.min(seekOffset, el.duration);
      }
      el.playbackRate = rate;
      if (wasPlaying.current) void el.play().catch(() => { /* автозапуск может быть запрещён */ });
    };
    if (el.readyState >= 1) apply();
    el.addEventListener("loadedmetadata", apply);
    return () => el.removeEventListener("loadedmetadata", apply);
    // rate намеренно не в зависимостях: его меняет свой обработчик, и
    // перезапуск этого эффекта на смене скорости дёргал бы перемотку.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src, seekOffset]);

  // Настенное время текущей позиции — наверх: по нему подписывается
  // шкала и берутся границы экспортируемого фрагмента.
  useEffect(() => {
    onClock(positionToClock(segments, index, pos));
  }, [segments, index, pos, onClock]);

  // Конец файла — не конец просмотра.
  const handleEnded = () => {
    const next = nextIndex(segments, index);
    if (next === null) {
      wasPlaying.current = false;
      return;
    }
    wasPlaying.current = true;
    onAdvance(next);
  };

  const step = useCallback((dir: 1 | -1) => {
    const el = video.current;
    if (!el) return;
    // Пауза обязательна: шаг по играющему видео сразу же перекрывается
    // обычным ходом воспроизведения, и кнопка выглядит неработающей.
    el.pause();
    // Якорь отсутствует, пока не показан ни один кадр (и в браузерах без
    // requestVideoFrameCallback) — тогда отсчёт от currentTime: шаг
    // получится менее точным, но кнопка обязана работать везде.
    const from = anchor.current ?? el.currentTime;
    el.currentTime = stepTime(from, frameDur.current, el.duration, dir);
  }, []);

  // Стрелки — то, чем покадровый просмотр пользуются на практике.
  // Слушатель на самом элементе, а не на документе: глобальный перехват
  // сломал бы стрелки в полях фильтров на этой же странице.
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
  };

  const changeRate = (value: number) => {
    const el = video.current;
    const next = (PLAYBACK_RATES.find(r => r === value) ?? DEFAULT_RATE) as PlaybackRate;
    setRate(next);
    if (el) el.playbackRate = next;
  };

  return (
    <div onKeyDown={onKeyDown} tabIndex={0} className="seg-player">
      <video
        ref={video}
        controls
        style={{ width: "100%", background: "#000" }}
        src={src}
        onPlay={() => { wasPlaying.current = true; }}
        onPause={() => { wasPlaying.current = false; }}
        onEnded={handleEnded}
        onTimeUpdate={e => setPos((e.target as HTMLVideoElement).currentTime)}
        onSeeked={e => setPos((e.target as HTMLVideoElement).currentTime)}
      />
      <div className="seg-player-bar">
        <button className="btn sm" onClick={() => step(-1)} title="Предыдущий кадр (←)">◀ кадр</button>
        <button className="btn sm" onClick={() => step(1)} title="Следующий кадр (→)">кадр ▶</button>
        <label style={{ margin: 0 }}>Скорость</label>
        <select value={rate} onChange={e => changeRate(Number(e.target.value))} style={{ width: 90 }}>
          {PLAYBACK_RATES.map(r => <option key={r} value={r}>{r}×</option>)}
        </select>
        <span className="empty" style={{ fontSize: 12 }}>{positionLabel(pos, frameDur.current)}</span>
      </div>
    </div>
  );
}

/** Полоса шкалы суток: где есть запись, где перерыв, где стоит плеер (ТЗ §5).
 *
 * Отсутствие записи здесь — не пустое место «по умолчанию», а показанное
 * состояние: подложка полосы окрашена как перерыв, а покрытие рисуется
 * поверх. Иначе «нет данных» и «нет записи» выглядели бы одинаково, а это
 * разные вещи: первое означает, что шкала не загрузилась.
 */
function TimelineStrip({ ranges, fromMs, toMs, playheadMs, onSeek }: {
  ranges: TimelineRange[];
  fromMs: number;
  toMs: number;
  playheadMs: number;
  onSeek: (ms: number) => void;
}) {
  const bars = useMemo(() => coverageBars(ranges, fromMs, toMs), [ranges, fromMs, toMs]);
  // Каждые два часа: 12 подписей на сутки читаются, 24 сливаются.
  const ticks = useMemo(
    () => Array.from({ length: 13 }, (_, i) => ({ pct: (i / 12) * 100, label: `${i * 2}:00` })),
    [],
  );
  const headPct = Number.isFinite(playheadMs)
    ? timeToFraction(playheadMs, fromMs, toMs) * 100 : null;

  const seek = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    if (rect.width <= 0) return;
    onSeek(fractionToTime((e.clientX - rect.left) / rect.width, fromMs, toMs));
  };

  return (
    <div className="timeline">
      <div className="timeline-strip" onClick={seek} title="Клик — перейти к этому времени">
        {bars.map((b, i) => (
          <div key={i} className="timeline-bar"
               style={{ left: `${b.leftPct}%`, width: `${b.widthPct}%` }} />
        ))}
        {ticks.slice(1, -1).map(t => (
          <div key={t.pct} className="timeline-tick" style={{ left: `${t.pct}%` }} />
        ))}
        {headPct !== null && (
          <div className="timeline-head" style={{ left: `${headPct}%` }} />
        )}
      </div>
      <div className="timeline-labels">
        {ticks.map(t => (
          <span key={t.pct} style={{ left: `${t.pct}%` }}>{t.label}</span>
        ))}
      </div>
    </div>
  );
}

export function Archive() {
  const [cams, setCams] = useState<any[]>([]);
  const [segs, setSegs] = useState<any[]>([]);
  const [f, setF] = useState({ camera_id: "", event_type: "", date_from: "", date_to: "", person_id: "" });
  const [sel, setSel] = useState<any | null>(null);
  // Границы экспортируемого фрагмента (ТЗ §5). Держатся отдельно от
  // фильтров поиска: оператор ищет по часам, а выгружает минуты.
  const [exp, setExp] = useState({ from: "", to: "" });
  // Сутки шкалы. В шкале архива, а не в поясе браузера: `video_segments`
  // хранит UTC, и «сегодня» здесь — те же сутки, что и в записях.
  const [day, setDay] = useState(() => new Date().toISOString().slice(0, 10));
  const [tl, setTl] = useState<{
    ranges: TimelineRange[]; segments: TimelineSegment[];
    recorded_sec: number; truncated: boolean;
  } | null>(null);
  const [tlError, setTlError] = useState("");
  // Позиция в цепочке: какой файл играет и с какой секунды в него встали.
  const [pos, setPos] = useState<Position | null>(null);
  const [clockMs, setClockMs] = useState(NaN);

  const { fromMs, toMs } = useMemo(() => dayWindow(day), [day]);

  useEffect(() => { api.cameras().then(setCams); search(); }, []);

  // Шкала перезагружается при смене камеры или суток. Без камеры её нет:
  // покрытие — это свойство одной камеры, а не объекта целиком.
  useEffect(() => {
    setTl(null);
    setPos(null);
    setClockMs(NaN);
    setTlError("");
    const camId = Number(f.camera_id);
    if (!camId) return;
    let cancelled = false;
    api.archiveTimeline(camId, formatArchiveTime(fromMs), formatArchiveTime(toMs))
      .then((r: any) => { if (!cancelled) setTl(r); })
      .catch((e: any) => { if (!cancelled) setTlError(String(e?.message ?? e)); });
    return () => { cancelled = true; };
  }, [f.camera_id, fromMs, toMs]);

  /** Клик по шкале или по строке таблицы — встать на это настенное время. */
  const seekTo = useCallback((ms: number) => {
    if (!tl) return;
    const p = locateAt(tl.segments, ms);
    // null — за последней записью суток: позиция не меняется, плеер не
    // гасится. Гасить его на промахе значило бы терять кадр, который
    // оператор уже нашёл.
    if (p) setPos(p);
  }, [tl]);

  // Выбор сегмента подставляет его границы как начальное окно экспорта —
  // дальше оператор сужает их до нужного события.
  //
  // Значения берутся из ответа API как есть и уходят обратно как есть, без
  // преобразования часовых поясов: столбцы `video_segments` наивные и
  // хранят UTC, и любой промежуточный `new Date()` сдвинул бы окно на
  // смещение пояса браузера — выгрузился бы не тот отрезок.
  useEffect(() => {
    if (!sel) return;
    setExp({ from: String(sel.started_at).slice(0, 19), to: String(sel.ended_at ?? "").slice(0, 19) });
    // Выбранная строка — это ещё и «покажи мне вот это место шкалы»:
    // камера и сутки подтягиваются под неё, а сама позиция ставится
    // эффектом ниже, когда шкала этих суток загрузится.
    setDay(String(sel.started_at).slice(0, 10));
    setF(prev => (String(prev.camera_id) === String(sel.camera_id)
      ? prev : { ...prev, camera_id: String(sel.camera_id) }));
  }, [sel]);

  // Позиция ставится не в обработчике клика, а здесь: между кликом и
  // ответом шкалы цепочки ещё нет, и вставать было бы не во что.
  useEffect(() => {
    if (!sel || !tl) return;
    const p = locateAt(tl.segments, parseArchiveTime(String(sel.started_at)));
    if (p) setPos(p);
  }, [sel, tl]);

  const search = async () => {
    const params: Record<string, string> = {};
    Object.entries(f).forEach(([k, v]) => { if (v) params[k] = v; });
    setSegs(await api.archive(params));
  };

  const url = (id: number) => `/api/archive/file/${id}`;

  // Камера, из архива которой собирается фрагмент: выбранная в фильтре
  // (со шкалой это основной путь) либо камера выбранной строки.
  const exportCamId = Number(f.camera_id) || Number(sel?.camera_id) || 0;

  const exportUrl = (cameraId: number, from: string, to: string) => {
    const q = new URLSearchParams({
      camera_id: String(cameraId),
      date_from: from,
      date_to: to,
      token: getToken() ?? "",
    });
    return `/api/archive/export?${q}`;
  };

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
            {/* Запись непрерывная (SPEC §5): с цикла 24 все новые сегменты
                идут с типом "continuous". "Движение"/"Лицо" оставлены для
                записей, сделанных прежним событийным слоем записи, — архив
                смешанный, пока они не выйдут за retention. */}
            <select value={f.event_type} onChange={e => setF({ ...f, event_type: e.target.value })}>
              <option value="">Все</option>
              <option value="continuous">Непрерывная запись</option>
              <option value="motion">Движение (архив до обновления)</option>
              <option value="face">Лицо (архив до обновления)</option>
            </select>
          </div>
          <div><label>С</label><input type="datetime-local" value={f.date_from} onChange={e => setF({ ...f, date_from: e.target.value })} /></div>
          <div><label>По</label><input type="datetime-local" value={f.date_to} onChange={e => setF({ ...f, date_to: e.target.value })} /></div>
        </div>
        <div style={{ marginTop: 8 }}>
          <label>ID персоны (опционально)</label>
          <input type="number" value={f.person_id} onChange={e => setF({ ...f, person_id: e.target.value })} style={{ width: 200 }} />
        </div>
        <button className="btn" style={{ marginTop: 8 }} onClick={search}>Найти</button>
      </div>

      <div className="grid" style={{ gridTemplateColumns: "1fr 1.4fr", gap: 16 }}>
        <div className="card">
          <h3>Найдено: {segs.length}</h3>
          <table>
            <thead><tr><th>Кадр</th><th>Время</th><th>Камера</th><th>Тип</th><th>Длит.</th></tr></thead>
            <tbody>
              {segs.map(s => (
                <tr key={s.id} style={{ cursor: "pointer", background: sel?.id === s.id ? "#222" : undefined }} onClick={() => setSel(s)}>
                  <td><SegThumb id={s.id} /></td>
                  <td>{new Date(s.started_at).toLocaleString("ru-RU")}</td>
                  <td>#{s.camera_id}</td>
                  <td>{s.event_type}</td>
                  <td>{s.duration_sec}с</td>
                </tr>
              ))}
              {segs.length === 0 && <tr><td colSpan={5} className="empty">Ничего не найдено</td></tr>}
            </tbody>
          </table>
        </div>
        <div className="card">
          {/* Шкала суток (ТЗ §5). Раньше плеер играл ровно один файл: до
              «что было в 03:40» оператор добирался перебором строк, а
              перерыв в записи по таблице не читался вовсе — отсутствие
              строки в списке не видно. */}
          <div style={{ marginBottom: 12 }}>
            <div className="seg-player-bar" style={{ marginBottom: 6 }}>
              <label style={{ margin: 0 }}>Сутки</label>
              <input type="date" value={day} onChange={e => setDay(e.target.value)}
                     style={{ width: 160 }} />
              <span className="empty" style={{ fontSize: 12 }}>
                {clockLabel(clockMs)}
              </span>
              {tl && (
                <span className="empty" style={{ fontSize: 12 }}>
                  записано: {recordedLabel(tl.recorded_sec)}
                </span>
              )}
            </div>
            {!f.camera_id && (
              <div className="empty">Выберите камеру — шкала строится по одной камере</div>
            )}
            {tlError && <div className="empty">Шкала недоступна: {tlError}</div>}
            {f.camera_id && tl && (
              <>
                <TimelineStrip ranges={tl.ranges} fromMs={fromMs} toMs={toMs}
                               playheadMs={clockMs} onSeek={seekTo} />
                {tl.truncated && (
                  <div className="empty" style={{ fontSize: 12 }}>
                    Сегментов за сутки больше, чем помещается в шкалу, — показано начало окна.
                  </div>
                )}
                {tl.segments.length === 0 && (
                  <div className="empty" style={{ fontSize: 12 }}>За эти сутки записи нет</div>
                )}
              </>
            )}
          </div>

          {pos && tl ? (
            <>
              <h3>
                Камера #{f.camera_id} · сегмент #{tl.segments[pos.index]?.id}
                {" "}<span className="empty" style={{ fontSize: 12, fontWeight: "normal" }}>
                  ({pos.index + 1} из {tl.segments.length}, воспроизведение идёт через границы файлов)
                </span>
              </h3>
              <ChainPlayer
                segments={tl.segments}
                index={pos.index}
                seekOffset={pos.offsetSec}
                onAdvance={next => setPos({ index: next, offsetSec: 0 })}
                onClock={setClockMs}
              />
              <a className="btn" href={`${url(tl.segments[pos.index].id)}?token=${getToken()}`}
                 download style={{ marginTop: 8, display: "inline-block" }}>
                Скачать MP4
              </a>
            </>
          ) : sel ? (
            <>
              <h3>Сегмент #{sel.id}</h3>
              <ChainPlayer
                segments={[{ id: sel.id, started_at: sel.started_at,
                             ended_at: sel.ended_at, duration_sec: sel.duration_sec }]}
                index={0}
                seekOffset={0}
                onAdvance={() => { /* цепочки нет — играется один файл */ }}
                onClock={setClockMs}
              />
              <a className="btn" href={`${url(sel.id)}?token=${getToken()}`} download style={{ marginTop: 8, display: "inline-block" }}>
                Скачать MP4
              </a>
            </>
          ) : null}

          {/* ТЗ §5 «экспорт фрагментов»: до этого архив умел отдавать
              только сегмент целиком, и событие на границе двух сегментов
              оператор склеивал вручную. Фрагмент собирается remux'ом
              поверх скольких угодно сегментов камеры.

              Блок держится на камере, а не на выбранной строке: со шкалой
              оператор доходит до нужной минуты, ни разу не тронув таблицу,
              и требовать от него выбрать строку ради экспорта значило бы
              вернуть перебор, который шкала и убрала. */}
          {exportCamId > 0 && (
            <div style={{ marginTop: 16, borderTop: "1px solid #333", paddingTop: 12 }}>
              <h4 style={{ margin: "0 0 8px" }}>Экспорт фрагмента</h4>
              <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                <div>
                  <label>С</label>
                  <input type="datetime-local" step="1" value={exp.from}
                         onChange={e => setExp({ ...exp, from: e.target.value })} />
                </div>
                <div>
                  <label>По</label>
                  <input type="datetime-local" step="1" value={exp.to}
                         onChange={e => setExp({ ...exp, to: e.target.value })} />
                </div>
              </div>
              {/* Границы с точки просмотра: оператор нашёл момент глазами,
                  и переписывать его руками в поле — лишний шаг, на котором
                  и ошибаются. */}
              <div className="seg-player-bar" style={{ marginTop: 6 }}>
                <button className="btn sm" disabled={!Number.isFinite(clockMs)}
                        onClick={() => setExp(p => ({ ...p, from: formatArchiveTime(clockMs) }))}>
                  Начало отсюда
                </button>
                <button className="btn sm" disabled={!Number.isFinite(clockMs)}
                        onClick={() => setExp(p => ({ ...p, to: formatArchiveTime(clockMs) }))}>
                  Конец здесь
                </button>
              </div>
              <a className="btn"
                 href={exportUrl(exportCamId, exp.from, exp.to)}
                 download
                 style={{
                   marginTop: 8, display: "inline-block",
                   // Пустые границы дали бы 422 от сервера; ссылка
                   // гасится до запроса.
                   pointerEvents: exp.from && exp.to ? undefined : "none",
                   opacity: exp.from && exp.to ? 1 : 0.5,
                 }}>
                Скачать фрагмент
              </a>
              <div className="empty" style={{ marginTop: 6, fontSize: 12 }}>
                Фрагмент склеивается из сегментов камеры без перекодирования,
                поэтому начало сдвигается к ближайшему опорному кадру —
                на 1–2 секунды раньше указанного.
              </div>
            </div>
          )}
          {!sel && !pos && <div className="empty">Выберите камеру и сутки или сегмент в списке</div>}
        </div>
      </div>
    </div>
  );
}

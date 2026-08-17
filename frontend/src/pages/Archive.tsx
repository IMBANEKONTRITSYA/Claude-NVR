import { useCallback, useEffect, useRef, useState } from "react";
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

/** Плеер архива с покадровым просмотром (ТЗ §5).
 *
 * Длительность кадра измеряется по самому потоку через
 * `requestVideoFrameCallback`: у камер §1 основной поток идёт 15–30 fps,
 * это настройка камеры, и в `video_segments` её нет. Константа вместо
 * замера означала бы, что на половине камер шаг перепрыгивает кадр или
 * топчется на месте.
 */
function SegmentPlayer({ src }: { src: string }) {
  const video = useRef<HTMLVideoElement | null>(null);
  const frameDur = useRef<number>(FALLBACK_FRAME_DURATION);
  const lastFrameTime = useRef<number | null>(null);
  // Точная граница начала показанного кадра. `currentTime` для этого не
  // годится: он отдаёт позицию воспроизведения где-то внутри кадра.
  const anchor = useRef<number | null>(null);
  const [rate, setRate] = useState<PlaybackRate>(DEFAULT_RATE);
  const [pos, setPos] = useState(0);

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

export function Archive() {
  const [cams, setCams] = useState<any[]>([]);
  const [segs, setSegs] = useState<any[]>([]);
  const [f, setF] = useState({ camera_id: "", event_type: "", date_from: "", date_to: "", person_id: "" });
  const [sel, setSel] = useState<any | null>(null);
  // Границы экспортируемого фрагмента (ТЗ §5). Держатся отдельно от
  // фильтров поиска: оператор ищет по часам, а выгружает минуты.
  const [exp, setExp] = useState({ from: "", to: "" });

  useEffect(() => { api.cameras().then(setCams); search(); }, []);

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
  }, [sel]);

  const search = async () => {
    const params: Record<string, string> = {};
    Object.entries(f).forEach(([k, v]) => { if (v) params[k] = v; });
    setSegs(await api.archive(params));
  };

  const url = (id: number) => `/api/archive/file/${id}`;

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
          {sel ? (
            <>
              <h3>Сегмент #{sel.id}</h3>
              <SegmentPlayer src={`${url(sel.id)}?token=${getToken()}`} />
              <a className="btn" href={`${url(sel.id)}?token=${getToken()}`} download style={{ marginTop: 8, display: "inline-block" }}>
                Скачать MP4
              </a>

              {/* ТЗ §5 «экспорт фрагментов»: до этого архив умел отдавать
                  только сегмент целиком, и событие на границе двух
                  сегментов оператор склеивал вручную. Фрагмент собирается
                  remux'ом поверх скольких угодно сегментов камеры. */}
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
                <a className="btn"
                   href={exportUrl(sel.camera_id, exp.from, exp.to)}
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
            </>
          ) : <div className="empty">Выберите сегмент</div>}
        </div>
      </div>
    </div>
  );
}

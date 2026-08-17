import { useEffect, useMemo, useRef, useState } from "react";
import Hls from "hls.js";
import { api, camSnapshotUrl, getRole } from "../api";
import { useWebSocket } from "../useWebSocket";
import { Pager } from "../Pager";
import {
  DEFAULT_LAYOUT, LAYOUTS, Layout, MIN_ZOOM, Pan,
  applyZoom, clampPage, clampPan, gridColumns, isLayout, pageSlice, zoomTransform,
} from "../liveLayout";
import { PtzHold, PtzVector } from "../ptz";
import { LiveDiagnosis, diagnoseLiveFailure } from "../liveDiagnostics";

type Box = { id: number; name: string; is_known: boolean; x: number; y: number; w: number; h: number; ts: number };
type Preset = { token: string; name: string };

const LAYOUT_KEY = "fw_live_layout";

// Скорость поворота и зума. Не максимум: на 1.0 купол проскакивает точку
// мимо, и оператор ловит её несколькими нажатиями туда-обратно.
const PTZ_SPEED = 0.6;
const PTZ_ZOOM_SPEED = 0.5;

const PTZ_BUTTONS: { label: string; title: string; v: PtzVector }[] = [
  { label: "↖", title: "Влево-вверх", v: { pan: -PTZ_SPEED, tilt: PTZ_SPEED } },
  { label: "↑", title: "Вверх", v: { tilt: PTZ_SPEED } },
  { label: "↗", title: "Вправо-вверх", v: { pan: PTZ_SPEED, tilt: PTZ_SPEED } },
  { label: "←", title: "Влево", v: { pan: -PTZ_SPEED } },
  { label: "·", title: "", v: {} },
  { label: "→", title: "Вправо", v: { pan: PTZ_SPEED } },
  { label: "↙", title: "Влево-вниз", v: { pan: -PTZ_SPEED, tilt: -PTZ_SPEED } },
  { label: "↓", title: "Вниз", v: { tilt: -PTZ_SPEED } },
  { label: "↘", title: "Вправо-вниз", v: { pan: PTZ_SPEED, tilt: -PTZ_SPEED } },
];

/** Пульт PTZ развёрнутой камеры (SPEC §4).
 *
 * Только для развёрнутой камеры, а не для каждой плитки мозаики: в сетке
 * 4×4 пульт негде разместить, и, что важнее, случайное нажатие увело бы
 * камеру, на которую оператор в этот момент даже не смотрит. */
function PtzPad({ camId }: { camId: number }) {
  const [supported, setSupported] = useState<boolean | null>(null);
  const [presets, setPresets] = useState<Preset[]>([]);
  const [profileToken, setProfileToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const holdRef = useRef<PtzHold | null>(null);

  useEffect(() => {
    let alive = true;
    setSupported(null);
    setError(null);
    api.camPtz(camId)
      .then((r: any) => {
        if (!alive) return;
        setSupported(!!r.supported);
        setPresets(r.presets || []);
        setProfileToken(r.profile_token ?? null);
        if (r.error) setError(r.error);
      })
      .catch(() => { if (alive) setSupported(false); });
    return () => { alive = false; };
  }, [camId]);

  // Контроллер удержания пересоздаётся вместе с камерой и обязательно
  // останавливает её при размонтировании: свернуть камеру в мозаику, не
  // отпустив стрелку, — самый простой способ оставить купол в движении.
  useEffect(() => {
    const hold = new PtzHold({
      move: v => api.camPtzMove(camId, { ...v, profile_token: profileToken }),
      stop: () => api.camPtzStop(camId, profileToken),
      onError: (e: any) => setError(e?.message || "Камера не приняла команду"),
    });
    holdRef.current = hold;
    return () => { hold.dispose(); holdRef.current = null; };
  }, [camId, profileToken]);

  // Отпускание кнопки вне её границ (курсор увели с пульта и отпустили над
  // видео или вообще за окном) до слушателя на самой кнопке не доходит —
  // pointerup ловится на окне.
  useEffect(() => {
    const stop = () => holdRef.current?.end();
    window.addEventListener("pointerup", stop);
    window.addEventListener("pointercancel", stop);
    window.addEventListener("blur", stop);
    return () => {
      window.removeEventListener("pointerup", stop);
      window.removeEventListener("pointercancel", stop);
      window.removeEventListener("blur", stop);
    };
  }, []);

  if (supported === null) return <div className="ptz-pad muted">Проверка PTZ…</div>;
  if (!supported) return null;

  const goto = async (token: string) => {
    setBusy(true);
    try {
      await api.camPtzGoto(camId, token, profileToken);
      setError(null);
    } catch (e: any) {
      setError(e?.message || "Не удалось перейти на позицию");
    } finally { setBusy(false); }
  };

  const savePreset = async () => {
    const name = window.prompt("Название позиции");
    if (!name?.trim()) return;
    setBusy(true);
    try {
      const r: any = await api.camPtzSavePreset(camId, name.trim(), profileToken);
      setPresets(r.presets || []);
      setError(null);
    } catch (e: any) {
      setError(e?.message || "Не удалось сохранить позицию");
    } finally { setBusy(false); }
  };

  // Нажатие останавливает всплытие: плитка под пультом разворачивает камеру
  // по клику и уходит в полноэкранный режим по двойному, а пультом кликают
  // часто и подряд.
  const holdProps = (v: PtzVector) => ({
    onPointerDown: (e: React.PointerEvent) => { e.stopPropagation(); holdRef.current?.start(v); },
    onPointerUp: (e: React.PointerEvent) => { e.stopPropagation(); holdRef.current?.end(); },
    onPointerLeave: () => holdRef.current?.end(),
    onClick: (e: React.MouseEvent) => e.stopPropagation(),
    onDoubleClick: (e: React.MouseEvent) => e.stopPropagation(),
  });

  return (
    <div className="ptz-pad" onClick={e => e.stopPropagation()}>
      <div className="ptz-grid">
        {PTZ_BUTTONS.map((b, i) => (
          b.title
            ? <button key={i} className="btn sm secondary ptz-btn" title={b.title} {...holdProps(b.v)}>{b.label}</button>
            : <span key={i} className="ptz-center" />
        ))}
      </div>
      <div className="ptz-zoom">
        <button className="btn sm secondary ptz-btn" title="Приблизить" {...holdProps({ zoom: PTZ_ZOOM_SPEED })}>+</button>
        <span className="muted">зум</span>
        <button className="btn sm secondary ptz-btn" title="Отдалить" {...holdProps({ zoom: -PTZ_ZOOM_SPEED })}>−</button>
      </div>
      <div className="ptz-presets">
        <select disabled={busy || presets.length === 0} value=""
          onChange={e => { if (e.target.value) goto(e.target.value); }}>
          <option value="">{presets.length ? "Позиция…" : "Позиций нет"}</option>
          {presets.map(p => <option key={p.token} value={p.token}>{p.name}</option>)}
        </select>
        <button className="btn sm secondary" disabled={busy} onClick={savePreset}
          title="Запомнить текущее положение камеры">Запомнить</button>
      </div>
      {error && <div className="ptz-error">{error}</div>}
    </div>
  );
}

function CameraTile({ cam, boxes, onClick }: any) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const hlsRef = useRef<Hls | null>(null);
  const tileRef = useRef<HTMLDivElement | null>(null);
  const [error, setError] = useState(false);
  // Почему плитка чёрная. Одной надписи «Камера недоступна» дежурному
  // мало: за ней прячутся и истёкшая сессия, и не заведённый слоем
  // записи путь, и кодек, который не тянет браузер, — и действия по ним
  // разные. Диагноз снимается с самого плейлиста, см. liveDiagnostics.ts.
  const [diagnosis, setDiagnosis] = useState<LiveDiagnosis | null>(null);
  // Снимок — запасной путь, когда HLS не играет. Если и он не отдался,
  // камера действительно недоступна, и только тогда честно говорим об этом.
  const [snapshotFailed, setSnapshotFailed] = useState(false);
  // Прямоугольник, который видео реально занимает внутри плитки. Нужен для
  // рамок лиц: при object-fit contain кадр вписывается с полями, и рамка,
  // посчитанная от размеров плитки, уехала бы на величину поля.
  const [videoRect, setVideoRect] = useState<{ l: number; t: number; w: number; h: number } | null>(null);
  // SPEC §4: цифровой зум. Состояние держится на плитке, а не на стене:
  // оператор увеличивает одну камеру, остальные при этом не трогает.
  const [zoom, setZoom] = useState(MIN_ZOOM);
  const [pan, setPan] = useState<Pan>({ x: 0, y: 0 });
  const drag = useRef<{ x: number; y: number; moved: boolean } | null>(null);

  // Двойной клик — настоящий браузерный полноэкранный режим
  const toggleFullscreen = () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else tileRef.current?.requestFullscreen().catch(() => {});
  };

  // Поток запрашивается ВСЕГДА, а не только при status === "online".
  //
  // Статус — производный признак: его выставляет воркер по данным Control
  // API MediaMTX, и он бывает устаревшим или неизвестным (воркер
  // перезапускается, медиасервер недоступен, камера только что заведена).
  // Пока показ видео был обусловлен статусом, любой сбой в этой цепочке
  // гасил всю стену камер, хотя потоки шли — пользователь ловил это дважды.
  //
  // Источник истины — сам плеер: поток либо играет, либо нет. Статус
  // остаётся подписью, а «Камера офлайн» показывается по фактической
  // ошибке воспроизведения.
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    const src = `/hls/cam${cam.id}/index.m3u8`;
    setError(false);
    setSnapshotFailed(false);
    setDiagnosis(null);

    // Плейлист запрашивается только после отказа воспроизведения, а не
    // заранее: на здоровой стене из 16 камер это 16 лишних запросов
    // каждый раз, а пользы от них нет — картинка и так идёт.
    let cancelled = false;
    const explain = async () => {
      setError(true);
      let playlistStatus = 0;
      let playlistBody: string | undefined;
      try {
        const r = await fetch(src, { cache: "no-store" });
        playlistStatus = r.status;
        if (r.ok) playlistBody = await r.text();
      } catch {
        playlistStatus = 0;
      }
      if (!cancelled) setDiagnosis(diagnoseLiveFailure({ playlistStatus, playlistBody }));
    };

    if (v.canPlayType("application/vnd.apple.mpegurl")) {
      v.src = src;
      v.play().catch(explain);
    } else if (Hls.isSupported()) {
      const hls = new Hls({ lowLatencyMode: true, liveSyncDuration: 1.5 });
      hlsRef.current = hls;
      hls.loadSource(src);
      hls.attachMedia(v);
      hls.on(Hls.Events.ERROR, (_e, data) => { if (data.fatal) explain(); });
    }
    return () => {
      cancelled = true;
      hlsRef.current?.destroy();
      hlsRef.current = null;
    };
  }, [cam.id]);

  // Пересчёт прямоугольника видео: при смене размера плитки (в том числе
  // при входе в полноэкранный режим) и когда стали известны размеры кадра.
  useEffect(() => {
    const v = videoRef.current;
    const box = tileRef.current;
    if (!v || !box) return;
    const recompute = () => {
      const vw = v.videoWidth, vh = v.videoHeight;
      const cw = box.clientWidth, ch = box.clientHeight;
      if (!vw || !vh || !cw || !ch) return setVideoRect(null);
      const scale = Math.min(cw / vw, ch / vh);
      const w = vw * scale, h = vh * scale;
      setVideoRect({ l: (cw - w) / 2, t: (ch - h) / 2, w, h });
    };
    v.addEventListener("loadedmetadata", recompute);
    v.addEventListener("resize", recompute);
    const ro = new ResizeObserver(recompute);
    ro.observe(box);
    recompute();
    return () => {
      v.removeEventListener("loadedmetadata", recompute);
      v.removeEventListener("resize", recompute);
      ro.disconnect();
    };
  }, [cam.id]);

  // Рамка лица приходит в долях кадра, поэтому кладётся на прямоугольник
  // видео, а не на плитку.
  const boxStyle = (b: Box) => {
    if (!videoRect) return { display: "none" as const };
    return {
      left: videoRect.l + b.x * videoRect.w,
      top: videoRect.t + b.y * videoRect.h,
      width: b.w * videoRect.w,
      height: b.h * videoRect.h,
      borderColor: b.is_known ? "var(--green)" : "var(--orange)",
    };
  };

  // Колесо мыши — привычный для NVR жест увеличения. preventDefault, иначе
  // страница уезжает под курсором вместо приближения кадра.
  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    setZoom(z => {
      const next = applyZoom(z, pan, e.deltaY < 0 ? 1.25 : 1 / 1.25);
      setPan(next.pan);
      return next.zoom;
    });
  };

  // Тянуть кадр можно только когда есть что тянуть (zoom > 1). Порог в
  // 3 пикселя отличает перетаскивание от клика: без него любое
  // микросмещение мыши разворачивало бы камеру на всю стену.
  const onPointerDown = (e: React.PointerEvent) => {
    if (zoom <= MIN_ZOOM) return;
    drag.current = { x: e.clientX, y: e.clientY, moved: false };
    (e.target as Element).setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current;
    const box = tileRef.current;
    if (!d || !box) return;
    const dx = (e.clientX - d.x) / box.clientWidth;
    const dy = (e.clientY - d.y) / box.clientHeight;
    if (Math.abs(e.clientX - d.x) > 3 || Math.abs(e.clientY - d.y) > 3) d.moved = true;
    d.x = e.clientX; d.y = e.clientY;
    setPan(p => ({ x: clampPan(p.x + dx, zoom), y: clampPan(p.y + dy, zoom) }));
  };
  const onPointerUp = () => { drag.current = null; };

  const resetZoom = () => { setZoom(MIN_ZOOM); setPan({ x: 0, y: 0 }); };

  const handleClick = () => {
    // Клик, завершивший перетаскивание, разворачивать камеру не должен.
    if (drag.current?.moved) return;
    onClick?.();
  };

  const transform = zoomTransform(zoom, pan);

  return (
    <div className="cam-tile" ref={tileRef} onClick={handleClick} onDoubleClick={toggleFullscreen}
      onWheel={onWheel} onPointerDown={onPointerDown} onPointerMove={onPointerMove}
      onPointerUp={onPointerUp} onPointerCancel={onPointerUp}
      style={{ cursor: zoom > MIN_ZOOM ? "grab" : undefined }}
      title="Клик — развернуть в сетке, двойной клик — на весь экран, колесо — цифровой зум">
      <div className="lbl">
        {cam.name} <span className={`badge ${cam.status}`}>{cam.status}</span>
      </div>
      {/* Зум двигает видео вместе с рамками лиц одной трансформацией:
          посчитай рамки отдельно — они разъехались бы с кадром. */}
      <div style={{ position: "absolute", inset: 0, overflow: "hidden", transform,
                    transformOrigin: "center center" }}>
        {/* objectFit: contain, а не cover: в видеонаблюдении обрезать часть
            кадра нельзя — с cover в полноэкранном режиме срезало края вместе
            с наложенными камерой датой и временем. Поля по бокам честнее
            потерянного куска кадра. */}
        <video ref={videoRef} muted autoPlay playsInline
          style={{ width: "100%", height: "100%", objectFit: "contain",
                   display: error ? "none" : "block" }} />
        {error && !snapshotFailed && (
          <img src={camSnapshotUrl(cam.id)} alt=""
            style={{ width: "100%", height: "100%", objectFit: "contain" }}
            onError={() => setSnapshotFailed(true)} />
        )}
        {boxes.map((b: Box) => (
          <div key={b.id} className="bbox" style={boxStyle(b)}>
            <span className="bbox-label" style={{ background: b.is_known ? "var(--green)" : "var(--orange)" }}>
              {b.name}
            </span>
          </div>
        ))}
      </div>
      {/* Причина показывается и поверх снимка: снимок из архива есть и
          тогда, когда живой поток не идёт, и без подписи плитка выглядит
          работающей — оператор смотрит на кадр минутной давности, не зная
          об этом. */}
      {error && (
        <div className="nostream">
          {diagnosis?.title ?? "Камера недоступна"}
          {diagnosis?.hint && <div className="nostream-hint">{diagnosis.hint}</div>}
        </div>
      )}
      {zoom > MIN_ZOOM && (
        <div className="zoom-badge" onClick={e => { e.stopPropagation(); resetZoom(); }}
          title="Сбросить цифровой зум">
          {zoom.toFixed(1)}× ✕
        </div>
      )}
    </div>
  );
}

export function LiveGrid() {
  const [cams, setCams] = useState<any[]>([]);
  const [full, setFull] = useState<number | null>(null);
  const [boxesByCam, setBoxesByCam] = useState<Record<number, Box[]>>({});
  // SPEC §4: «грид: 1, 4, 9, 16 камер на экран». Раскладка запоминается —
  // дежурный держит свою стену между сменами вкладок и перезагрузками.
  const [layout, setLayout] = useState<Layout>(() => {
    const saved = Number(localStorage.getItem(LAYOUT_KEY));
    return isLayout(saved) ? saved : DEFAULT_LAYOUT;
  });
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState("");
  // §18: наблюдателю матрица прав оставляет только просмотр — поворот камеры
  // меняет обзор для всех остальных операторов, а не только его картинку.
  const canPtz = ["admin", "operator"].includes(getRole());

  useEffect(() => { localStorage.setItem(LAYOUT_KEY, String(layout)); }, [layout]);

  useEffect(() => {
    api.cameras().then(setCams).catch(() => {});
    const t = setInterval(() => api.cameras().then(setCams).catch(() => {}), 5000);
    return () => clearInterval(t);
  }, []);

  useWebSocket("/ws/faces", (msg) => {
    // "face" — полноценное событие, "box" — лёгкий оверлей между событиями
    if ((msg.type !== "face" && msg.type !== "box") || !msg.bbox || !msg.frame_w) return;
    const b: Box = {
      id: msg.event_id ?? Math.random(), name: msg.name, is_known: msg.is_known,
      x: msg.bbox.x1 / msg.frame_w, y: msg.bbox.y1 / msg.frame_h,
      w: (msg.bbox.x2 - msg.bbox.x1) / msg.frame_w,
      h: (msg.bbox.y2 - msg.bbox.y1) / msg.frame_h,
      ts: Date.now(),
    };
    setBoxesByCam(prev => ({ ...prev, [msg.camera_id]: [...(prev[msg.camera_id] || []), b].slice(-10) }));
  });

  // Удаляем устаревшие рамки (старше 3 сек)
  useEffect(() => {
    const t = setInterval(() => {
      const cutoff = Date.now() - 3000;
      setBoxesByCam(prev => {
        const next: Record<number, Box[]> = {};
        for (const [k, v] of Object.entries(prev)) {
          const fresh = v.filter(b => b.ts > cutoff);
          if (fresh.length) next[+k] = fresh;
        }
        return next;
      });
    }, 1000);
    return () => clearInterval(t);
  }, []);

  // Фильтр по имени и локации — способ добраться до нужной камеры на
  // объекте, где страниц два десятка (§4: «переключение между камерами
  // без перезагрузки страницы»).
  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return q ? cams.filter(c => `${c.name} ${c.location}`.toLowerCase().includes(q)) : cams;
  }, [cams, filter]);

  // Номер страницы приводится к существующим при смене раскладки, фильтра
  // или числа камер — иначе стена молча оказывается пустой.
  const safePage = clampPage(page, filtered.length, layout);
  useEffect(() => { if (safePage !== page) setPage(safePage); }, [safePage, page]);

  // Развёрнутая камера — это ровно одна плитка, остальные плееры при этом
  // не держатся: смысл разворота в том, чтобы отдать ей всю полосу.
  const single = full !== null ? filtered.find(c => c.id === full) : undefined;
  const display = single ? [single] : pageSlice(filtered, safePage, layout);
  const cols = single ? 1 : gridColumns(layout);

  return (
    <div>
      <h2>Камеры в реальном времени</h2>
      <div className="toolbar">
        <span className="muted">Мозаика:</span>
        {LAYOUTS.map(n => (
          <button key={n} className={`btn sm ${layout === n && !single ? "" : "secondary"}`}
            onClick={() => { setFull(null); setLayout(n); }}>
            {n === 1 ? "1 камера" : `${n} камер`}
          </button>
        ))}
        {single && (
          <button className="btn sm secondary" onClick={() => setFull(null)}>
            ← Вернуться к мозаике
          </button>
        )}
        <input value={filter} onChange={e => { setFilter(e.target.value); setPage(1); }}
          placeholder="Фильтр по имени или локации"
          style={{ marginLeft: "auto", maxWidth: 260 }} />
      </div>
      <div className="muted" style={{ marginBottom: 12 }}>
        Клик по плитке — развернуть камеру, двойной клик — полный экран,
        колесо мыши — цифровой зум (кадр тянется мышью). Поток отдаётся
        MediaMTX по HLS, рамки лиц — через WebSocket.
      </div>
      <div className="cam-mosaic" style={{ gridTemplateColumns: `repeat(${cols}, 1fr)` }}>
        {display.map(c => (
          <CameraTile key={c.id} cam={c} boxes={boxesByCam[c.id] || []} onClick={() => setFull(full === c.id ? null : c.id)} />
        ))}
        {filtered.length === 0 && (
          <div className="empty">{cams.length === 0 ? "Камеры не добавлены" : "Нет камер по фильтру"}</div>
        )}
      </div>
      {/* Пульт PTZ — только у развёрнутой камеры и только для ролей, которым
          разрешено ею управлять. Кнопка, спрятанная от наблюдателя, не
          заменяет проверку роли на сервере (она есть в require_role), но
          показывать орган управления тому, кто получит на него 403, —
          отдельный вид неудобства. Камера без ONVIF PTZ не поддерживает
          физически: сам пульт ещё и спрашивает камеру, поворотная ли она. */}
      {single && single.has_onvif && canPtz && <PtzPad camId={single.id} />}
      {/* Пагинация, а не бесконечная стена: на объекте из 250 камер (§1)
          одновременно живут максимум 16 HLS-плееров. */}
      {!single && (
        <Pager page={safePage} pageSize={layout} total={filtered.length} onPage={setPage} />
      )}
    </div>
  );
}

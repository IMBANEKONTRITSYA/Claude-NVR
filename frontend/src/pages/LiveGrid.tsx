import { useEffect, useRef, useState } from "react";
import Hls from "hls.js";
import { api, camSnapshotUrl } from "../api";
import { useWebSocket } from "../useWebSocket";

type Box = { id: number; name: string; is_known: boolean; x: number; y: number; w: number; h: number; ts: number };

function CameraTile({ cam, boxes, onClick }: any) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const hlsRef = useRef<Hls | null>(null);
  const tileRef = useRef<HTMLDivElement | null>(null);
  const [error, setError] = useState(false);
  // Снимок — запасной путь, когда HLS не играет. Если и он не отдался,
  // камера действительно недоступна, и только тогда честно говорим об этом.
  const [snapshotFailed, setSnapshotFailed] = useState(false);
  // Прямоугольник, который видео реально занимает внутри плитки. Нужен для
  // рамок лиц: при object-fit contain кадр вписывается с полями, и рамка,
  // посчитанная от размеров плитки, уехала бы на величину поля.
  const [videoRect, setVideoRect] = useState<{ l: number; t: number; w: number; h: number } | null>(null);

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

    if (v.canPlayType("application/vnd.apple.mpegurl")) {
      v.src = src;
      v.play().catch(() => setError(true));
    } else if (Hls.isSupported()) {
      const hls = new Hls({ lowLatencyMode: true, liveSyncDuration: 1.5 });
      hlsRef.current = hls;
      hls.loadSource(src);
      hls.attachMedia(v);
      hls.on(Hls.Events.ERROR, (_e, data) => { if (data.fatal) setError(true); });
    }
    return () => { hlsRef.current?.destroy(); hlsRef.current = null; };
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

  return (
    <div className="cam-tile" ref={tileRef} onClick={onClick} onDoubleClick={toggleFullscreen}
      title="Клик — развернуть в сетке, двойной клик — на весь экран">
      <div className="lbl">
        {cam.name} <span className={`badge ${cam.status}`}>{cam.status}</span>
      </div>
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
      {error && snapshotFailed && <div className="nostream">Камера недоступна</div>}
      {boxes.map((b: Box) => (
        <div key={b.id} className="bbox" style={boxStyle(b)}>
          <span className="bbox-label" style={{ background: b.is_known ? "var(--green)" : "var(--orange)" }}>
            {b.name}
          </span>
        </div>
      ))}
    </div>
  );
}

export function LiveGrid() {
  const [cams, setCams] = useState<any[]>([]);
  const [full, setFull] = useState<number | null>(null);
  const [boxesByCam, setBoxesByCam] = useState<Record<number, Box[]>>({});

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

  const n = cams.length;
  const cols = n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4;
  const display = full !== null ? cams.filter(c => c.id === full) : cams;

  return (
    <div>
      <h2>Камеры в реальном времени</h2>
      <div className="muted" style={{ marginBottom: 12 }}>
        Клик по плитке — полноэкранный режим. Поток отдаётся MediaMTX по HLS, рамки лиц — через WebSocket.
      </div>
      <div className="cam-mosaic" style={{ gridTemplateColumns: `repeat(${full !== null ? 1 : cols}, 1fr)` }}>
        {display.map(c => (
          <CameraTile key={c.id} cam={c} boxes={boxesByCam[c.id] || []} onClick={() => setFull(full === c.id ? null : c.id)} />
        ))}
        {cams.length === 0 && <div className="empty">Камеры не добавлены</div>}
      </div>
    </div>
  );
}

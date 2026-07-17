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

  // Двойной клик — настоящий браузерный полноэкранный режим
  const toggleFullscreen = () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else tileRef.current?.requestFullscreen().catch(() => {});
  };

  useEffect(() => {
    if (cam.status !== "online" || !videoRef.current) return;
    const v = videoRef.current;
    const src = `/hls/cam${cam.id}/index.m3u8`;
    setError(false);

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
  }, [cam.id, cam.status]);

  const online = cam.status === "online";

  return (
    <div className="cam-tile" ref={tileRef} onClick={onClick} onDoubleClick={toggleFullscreen}
      title="Клик — развернуть в сетке, двойной клик — на весь экран">
      <div className="lbl">
        {cam.name} <span className={`badge ${cam.status}`}>{cam.status}</span>
      </div>
      {online && !error ? (
        <video ref={videoRef} muted autoPlay playsInline style={{ width: "100%", height: "100%", objectFit: "cover" }} />
      ) : online && error ? (
        <img src={camSnapshotUrl(cam.id)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
      ) : (
        <div className="nostream">Камера офлайн</div>
      )}
      {online && boxes.map((b: Box) => (
        <div key={b.id} className="bbox" style={{
          left: `${b.x * 100}%`, top: `${b.y * 100}%`,
          width: `${b.w * 100}%`, height: `${b.h * 100}%`,
          borderColor: b.is_known ? "var(--green)" : "var(--orange)",
        }}>
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
    if (msg.type !== "face" || !msg.bbox || !msg.frame_w) return;
    const b: Box = {
      id: msg.event_id, name: msg.name, is_known: msg.is_known,
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

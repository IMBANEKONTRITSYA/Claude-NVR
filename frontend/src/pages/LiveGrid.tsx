import { useEffect, useRef, useState } from "react";
import Hls from "hls.js";
import { api, camSnapshotUrl } from "../api";

function CameraTile({ cam, full, onClick }: any) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const hlsRef = useRef<Hls | null>(null);
  const [error, setError] = useState(false);

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

  return (
    <div className="cam-tile" onClick={onClick}>
      <div className="lbl">
        {cam.name} <span className={`badge ${cam.status}`}>{cam.status}</span>
      </div>
      {cam.status === "online" && !error ? (
        <video ref={videoRef} muted autoPlay playsInline style={{ width: "100%", height: "100%", objectFit: "cover" }} />
      ) : cam.status === "online" && error ? (
        <img src={camSnapshotUrl(cam.id)} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
      ) : (
        <div className="nostream">Камера офлайн</div>
      )}
    </div>
  );
}

export function LiveGrid() {
  const [cams, setCams] = useState<any[]>([]);
  const [full, setFull] = useState<number | null>(null);

  useEffect(() => {
    api.cameras().then(setCams).catch(() => {});
    const t = setInterval(() => api.cameras().then(setCams).catch(() => {}), 5000);
    return () => clearInterval(t);
  }, []);

  const n = cams.length;
  const cols = n <= 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4;
  const display = full !== null ? cams.filter(c => c.id === full) : cams;

  return (
    <div>
      <h2>Камеры в реальном времени</h2>
      <div className="muted" style={{ marginBottom: 12 }}>
        Клик по плитке — полноэкранный режим. Поток отдаётся MediaMTX по HLS.
      </div>
      <div className="cam-mosaic" style={{ gridTemplateColumns: `repeat(${full !== null ? 1 : cols}, 1fr)` }}>
        {display.map(c => (
          <CameraTile key={c.id} cam={c} full={full === c.id} onClick={() => setFull(full === c.id ? null : c.id)} />
        ))}
        {cams.length === 0 && <div className="empty">Камеры не добавлены</div>}
      </div>
    </div>
  );
}

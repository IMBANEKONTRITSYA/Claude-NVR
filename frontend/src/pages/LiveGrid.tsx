import { useEffect, useState } from "react";
import { api } from "../api";

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
      <p className="muted">
        Поток MJPEG/HLS подключается через MediaMTX. В текущей сборке отображается заглушка статуса —
        интеграция HLS-плеера hls.js добавляется при подключении реальных камер.
      </p>
      <div className="cam-mosaic" style={{ gridTemplateColumns: `repeat(${full !== null ? 1 : cols}, 1fr)` }}>
        {display.map(c => (
          <div key={c.id} className="cam-tile" onClick={() => setFull(full === c.id ? null : c.id)}>
            <div className="lbl">
              {c.name} <span className={`badge ${c.status}`}>{c.status}</span>
            </div>
            <div className="nostream">
              {c.status === "online" ? "Поток подключается..." : "Камера офлайн"}
            </div>
          </div>
        ))}
        {cams.length === 0 && <div className="empty">Камеры не добавлены</div>}
      </div>
    </div>
  );
}

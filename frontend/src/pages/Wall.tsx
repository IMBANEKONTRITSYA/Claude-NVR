import { useEffect, useRef, useState } from "react";
import { api, mediaUrl, wsUrl } from "../api";

export function Wall() {
  const [items, setItems] = useState<any[]>([]);
  const [paused, setPaused] = useState(false);
  const [filter, setFilter] = useState<"all" | "known" | "unknown">("all");
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    api.events(60).then((evs: any[]) => {
      setItems(evs.map(e => ({
        event_id: e.id, camera_id: e.camera_id, person_id: e.person_id,
        name: e.name,
        is_known: e.is_known, snapshot: e.snapshot_path, ts: e.ts,
      })));
    });
    const ws = new WebSocket(wsUrl("/ws/faces"));
    wsRef.current = ws;
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.type !== "face") return;
      setItems(prev => paused ? prev : [msg, ...prev].slice(0, 200));
    };
    return () => ws.close();
  }, [paused]);

  const ago = (iso: string) => {
    const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
    if (s < 60) return `${s} сек`;
    if (s < 3600) return `${Math.floor(s / 60)} мин`;
    return `${Math.floor(s / 3600)} ч`;
  };

  const visible = items.filter(i =>
    filter === "all" || (filter === "known" && i.is_known) || (filter === "unknown" && !i.is_known)
  );

  return (
    <div>
      <h2>Стена распознавания</h2>
      <div className="toolbar">
        <button className="btn secondary" onClick={() => setPaused(p => !p)}>{paused ? "Возобновить" : "Пауза"}</button>
        <select value={filter} onChange={e => setFilter(e.target.value as any)} style={{ width: 200 }}>
          <option value="all">Все</option>
          <option value="known">Только известные</option>
          <option value="unknown">Только неизвестные</option>
        </select>
      </div>
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))" }}>
        {visible.map((i, k) => (
          <div key={`${i.event_id}-${k}`} className={`tile ${i.is_known ? "known" : "unknown"}`}>
            {i.snapshot ? <img src={mediaUrl(i.snapshot)} /> : <div style={{ width: 64, height: 64, background: "#000" }} />}
            <div>
              <div>{i.name}</div>
              <div className="muted" style={{ fontSize: 12 }}>Камера #{i.camera_id} · {ago(i.ts)}</div>
            </div>
          </div>
        ))}
        {visible.length === 0 && <div className="empty">Пока нет событий</div>}
      </div>
    </div>
  );
}

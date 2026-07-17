import { useEffect, useRef, useState, useCallback } from "react";
import { api, mediaUrl } from "../api";
import { useWebSocket } from "../useWebSocket";

const PAGE = 60;

function mapEvent(e: any) {
  return {
    event_id: e.id, camera_id: e.camera_id, person_id: e.person_id,
    name: e.name, is_known: e.is_known, snapshot: e.snapshot_path, ts: e.ts,
  };
}

export function Wall() {
  const [items, setItems] = useState<any[]>([]);
  const [paused, setPaused] = useState(false);
  const [filter, setFilter] = useState<"all" | "known" | "unknown">("all");
  const [hasMore, setHasMore] = useState(true);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  const loadingRef = useRef(false);
  const itemsRef = useRef<any[]>([]);
  itemsRef.current = items;
  const hasMoreRef = useRef(true);
  hasMoreRef.current = hasMore;
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    api.events(PAGE).then((evs: any[]) => {
      setItems(evs.map(mapEvent));
      if (evs.length < PAGE) setHasMore(false);
    });
  }, []);

  // Бесконечный скролл: подгружаем историю, когда sentinel попадает в вьюпорт
  const loadMore = useCallback(async () => {
    if (loadingRef.current || !hasMoreRef.current) return;
    const cur = itemsRef.current;
    if (cur.length === 0) return;
    loadingRef.current = true;
    try {
      const minId = Math.min(...cur.map(i => i.event_id));
      const evs: any[] = await api.raw(`/api/events?limit=${PAGE}&before_id=${minId}`);
      if (evs.length < PAGE) setHasMore(false);
      if (evs.length) {
        setItems(prev => {
          const seen = new Set(prev.map(i => i.event_id));
          return [...prev, ...evs.map(mapEvent).filter(i => !seen.has(i.event_id))];
        });
      }
    } catch {}
    loadingRef.current = false;
  }, []);

  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const obs = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting) loadMore();
    }, { rootMargin: "400px" });
    obs.observe(el);
    return () => obs.disconnect();
  }, [loadMore]);

  useWebSocket("/ws/faces", (msg) => {
    if (msg.type === "face") {
      setItems(prev => pausedRef.current ? prev : [msg, ...prev]);
    } else if (msg.type === "enhanced") {
      // Подменяем фото на улучшенное по event_id
      setItems(prev => prev.map(i => i.event_id === msg.event_id ? { ...i, snapshot: msg.snapshot } : i));
    }
  });

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
        <span className="muted">Показано: {visible.length}</span>
      </div>
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))" }}>
        {visible.map((i, k) => (
          <div key={`${i.event_id}-${k}`} className={`tile ${i.is_known ? "known" : "unknown"}`}>
            {i.snapshot ? <img src={mediaUrl(i.snapshot)} loading="lazy" /> : <div style={{ width: 64, height: 64, background: "#000" }} />}
            <div>
              <div>{i.name}</div>
              <div className="muted" style={{ fontSize: 12 }}>Камера #{i.camera_id} · {ago(i.ts)}</div>
            </div>
          </div>
        ))}
        {visible.length === 0 && <div className="empty">Пока нет событий</div>}
      </div>
      <div ref={sentinelRef} style={{ height: 1 }} />
      {!hasMore && items.length > 0 && <div className="empty">История загружена полностью</div>}
    </div>
  );
}

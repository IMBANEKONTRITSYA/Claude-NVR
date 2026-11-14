import { useEffect, useRef } from "react";
import { wsUrl } from "./api";

/**
 * Подписка на WebSocket с автопереподключением (exponential backoff: 1→2→4→8s, max 30s).
 */
export function useWebSocket(path: string, onMessage: (data: any) => void, deps: any[] = []) {
  const wsRef = useRef<WebSocket | null>(null);
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;

  useEffect(() => {
    let closed = false;
    let backoff = 1000;
    let timer: any = null;

    const connect = () => {
      if (closed) return;
      const ws = new WebSocket(wsUrl(path));
      wsRef.current = ws;
      ws.onopen = () => { backoff = 1000; };
      ws.onmessage = (m) => {
        try { handlerRef.current(JSON.parse(m.data)); } catch {}
      };
      ws.onclose = () => {
        if (closed) return;
        timer = setTimeout(connect, backoff);
        backoff = Math.min(backoff * 2, 30000);
      };
      ws.onerror = () => { try { ws.close(); } catch {} };
    };
    connect();

    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      try { wsRef.current?.close(); } catch {}
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

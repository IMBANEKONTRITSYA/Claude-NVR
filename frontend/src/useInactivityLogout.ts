import { useEffect } from "react";

// ТЗ 13: "автоматический logout при неактивности". Отсчитывается по реальному
// взаимодействию пользователя (мышь/клавиатура/касание), а не по фоновым
// запросам (WebSocket живого просмотра сам по себе не должен продлевать
// сессию бесконечно).
const INACTIVITY_TIMEOUT_MS = 15 * 60 * 1000; // 15 минут
const ACTIVITY_EVENTS = ["mousedown", "mousemove", "keydown", "wheel", "touchstart"] as const;

export function useInactivityLogout(enabled: boolean, onTimeout: () => void) {
  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setTimeout>;
    const reset = () => {
      clearTimeout(timer);
      timer = setTimeout(onTimeout, INACTIVITY_TIMEOUT_MS);
    };
    reset();
    ACTIVITY_EVENTS.forEach(ev => window.addEventListener(ev, reset, { passive: true }));
    return () => {
      clearTimeout(timer);
      ACTIVITY_EVENTS.forEach(ev => window.removeEventListener(ev, reset));
    };
  }, [enabled, onTimeout]);
}

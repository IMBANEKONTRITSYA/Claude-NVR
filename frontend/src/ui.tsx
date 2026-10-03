import { createContext, useContext, useState, useCallback, useEffect } from "react";

type ToastType = "info" | "ok" | "err" | "warn";
interface Toast { id: number; type: ToastType; text: string }
interface Confirm { id: number; text: string; resolve: (v: boolean) => void }

interface Ctx {
  toast: (text: string, type?: ToastType) => void;
  confirm: (text: string) => Promise<boolean>;
}

const C = createContext<Ctx | null>(null);

export function useUI(): Ctx {
  const v = useContext(C);
  if (!v) throw new Error("useUI вне UIProvider");
  return v;
}

// Модульный счётчик: useState(0)[0] в замыкании всегда давал бы id=1 для всех
// тостов — дублирующиеся ключи, и первый таймер закрывал бы все тосты разом.
let seq = 0;

export function UIProvider({ children }: { children: any }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [confirms, setConfirms] = useState<Confirm[]>([]);

  const toast = useCallback((text: string, type: ToastType = "info") => {
    const id = ++seq;
    setToasts(t => [...t, { id, type, text }]);
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 4000);
  }, []);

  const confirm = useCallback((text: string) => new Promise<boolean>(resolve => {
    setConfirms(c => [...c, { id: ++seq, text, resolve }]);
  }), []);

  const close = (id: number, value: boolean) => {
    setConfirms(c => {
      const target = c.find(x => x.id === id);
      target?.resolve(value);
      return c.filter(x => x.id !== id);
    });
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (confirms.length === 0) return;
      if (e.key === "Escape") close(confirms[confirms.length - 1].id, false);
      if (e.key === "Enter") close(confirms[confirms.length - 1].id, true);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirms]);

  return (
    <C.Provider value={{ toast, confirm }}>
      {children}
      <div className="toasts">
        {toasts.map(t => (
          <div key={t.id} className={`toast toast-${t.type}`}>{t.text}</div>
        ))}
      </div>
      {confirms.map(c => (
        <div key={c.id} className="modal-backdrop" onClick={() => close(c.id, false)}>
          <div className="modal" onClick={e => e.stopPropagation()}>
            <div style={{ marginBottom: 16 }}>{c.text}</div>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button className="btn secondary" onClick={() => close(c.id, false)}>Отмена</button>
              <button className="btn danger" autoFocus onClick={() => close(c.id, true)}>Подтвердить</button>
            </div>
          </div>
        </div>
      ))}
    </C.Provider>
  );
}

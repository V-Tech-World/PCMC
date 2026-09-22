import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from "react";

/**
 * Toast notifications: auto-dismissing slide-ins in the top-right corner.
 * Actions (save, review, dial attempt...) push here instead of mutating
 * inline banners, so the screen stays calm and feedback is instant.
 */
type Kind = "ok" | "error" | "info";

interface ToastItem {
  id: number;
  kind: Kind;
  text: string;
}

const ToastContext = createContext<{
  push: (kind: Kind, text: string) => void;
} | null>(null);

const PANEL: Record<Kind, string> = {
  ok: "bg-emerald-50 text-emerald-800 ring-emerald-200 dark:bg-emerald-950/90 dark:text-emerald-200 dark:ring-emerald-800",
  error:
    "bg-red-50 text-red-700 ring-red-200 dark:bg-red-950/90 dark:text-red-200 dark:ring-red-800",
  info: "bg-brand-50 text-brand-800 ring-brand-200 dark:bg-[#182219]/90 dark:text-brand-200 dark:ring-brand-700",
};

const BADGE: Record<Kind, string> = {
  ok: "bg-emerald-500 text-white",
  error: "bg-red-500 text-white",
  info: "bg-brand-600 text-white",
};

const GLYPH: Record<Kind, string> = { ok: "✓", error: "!", info: "i" };

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  const push = useCallback((kind: Kind, text: string) => {
    const id = Date.now() + Math.random();
    setToasts((list) => [...list, { id, kind, text }]);
    window.setTimeout(() => {
      setToasts((list) => list.filter((t) => t.id !== id));
    }, 4500);
  }, []);

  const dismiss = (id: number) =>
    setToasts((list) => list.filter((t) => t.id !== id));

  return (
    <ToastContext.Provider value={{ push }}>
      {children}
      <div
        className="pointer-events-none fixed right-4 top-4 z-[60] flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2"
        aria-live="polite"
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            role="status"
            className={`toast-in pointer-events-auto flex items-start gap-3 rounded-xl px-4 py-3 text-sm font-medium shadow-lg ring-1 ${PANEL[t.kind]}`}
          >
            <span
              className={`grid h-5 w-5 shrink-0 place-items-center rounded-full text-[11px] font-black ${BADGE[t.kind]}`}
              aria-hidden="true"
            >
              {GLYPH[t.kind]}
            </span>
            <span className="flex-1 leading-snug">{t.text}</span>
            <button
              type="button"
              onClick={() => dismiss(t.id)}
              aria-label="Dismiss notification"
              className="cursor-pointer rounded p-0.5 text-black/35 transition-colors hover:text-black/70 dark:text-white/40 dark:hover:text-white/80"
            >
              ✕
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return {
    ok: (text: string) => ctx.push("ok", text),
    error: (text: string) => ctx.push("error", text),
    info: (text: string) => ctx.push("info", text),
  };
}
import { useEffect, useState, type ReactNode } from "react";
import type { RiskLevel } from "../lib/types";

/** TC2: risk-level colour coding used across every screen. */
const RISK_STYLES: Record<RiskLevel, string> = {
  high: "bg-red-100 text-red-700 ring-2 ring-red-200 dark:bg-red-950 dark:text-red-300 dark:ring-red-800",
  medium:
    "bg-amber-100 text-amber-800 ring-2 ring-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:ring-amber-800",
  low: "bg-emerald-100 text-emerald-700 ring-2 ring-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-800",
  unknown:
    "bg-slate-100 text-slate-600 ring-2 ring-slate-200 dark:bg-neutral-800 dark:text-neutral-300 dark:ring-neutral-700",
};

export function RiskBadge({
  level,
  score,
}: {
  level: RiskLevel;
  score?: number;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-bold uppercase tracking-wide ${
        RISK_STYLES[level] ?? RISK_STYLES.unknown
      }`}
    >
      {level}
      {score !== undefined && (
        <span className="font-semibold opacity-70">{score.toFixed(0)}</span>
      )}
    </span>
  );
}

/** Lightweight count-up for card numbers (respects reduced-motion). */
function useCountUp(target: number): number {
  const [value, setValue] = useState(0);
  useEffect(() => {
    if (target <= 0) {
      setValue(target);
      return;
    }
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setValue(target);
      return;
    }
    let raf = 0;
    const start = performance.now();
    const duration = 650;
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / duration);
      setValue(Math.round(target * (1 - Math.pow(1 - p, 3))));
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target]);
  return value;
}

/** One of the six green cards from the dashboard sketch (with icon +
 * count-up + hover lift). */
export function StatCard({
  label,
  value,
  hint,
  icon,
}: {
  label: string;
  value: string | number;
  hint?: string;
  icon?: ReactNode;
}) {
  const isNumber = typeof value === "number";
  const shown = useCountUp(isNumber ? value : 0);
  return (
    <div className="group relative overflow-hidden rounded-2xl bg-gradient-to-br from-brand-500 to-brand-700 p-5 text-white shadow-md transition-all duration-200 hover:-translate-y-0.5 hover:shadow-xl">
      <div
        className="absolute -right-4 -top-4 h-20 w-20 rounded-full bg-white/10 transition-transform duration-300 group-hover:scale-125"
        aria-hidden="true"
      />
      <div className="flex items-start justify-between gap-3">
        <div className="text-3xl font-extrabold leading-none">
          {isNumber ? shown : String(value)}
        </div>
        {icon && (
          <span className="rounded-xl bg-white/15 p-2 text-white/90">
            {icon}
          </span>
        )}
      </div>
      <div className="mt-2 text-sm font-semibold text-white/90">{label}</div>
      {hint && <div className="mt-1 text-xs text-white/70">{hint}</div>}
    </div>
  );
}

/** White panel with an optional header row. */
export function Card({
  title,
  children,
  actions,
}: {
  title?: string;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="rounded-xl bg-white shadow-sm ring-1 ring-neutral-200 dark:bg-[#1a201d] dark:ring-neutral-700">
      {(title || actions) && (
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-neutral-100 px-4 py-3 dark:border-neutral-700/70">
          <h2 className="text-xs font-bold uppercase tracking-wider text-neutral-500 dark:text-neutral-400">
            {title}
          </h2>
          {actions}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

/** Result banner (dial started, save ok, errors...). */
export function Banner({
  kind,
  children,
}: {
  kind: "ok" | "error" | "info";
  children: ReactNode;
}) {
  const styles = {
    ok: "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/70 dark:text-emerald-300",
    error:
      "border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-950/70 dark:text-red-300",
    info: "border-brand-200 bg-brand-50 text-brand-800 dark:border-brand-700 dark:bg-[#15251c] dark:text-brand-200",
  } as const;
  return (
    <div className={`rounded-lg border px-4 py-3 text-sm font-medium ${styles[kind]}`}>
      {children}
    </div>
  );
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return "--";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "--";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function fmtDuration(sec: number): string {
  if (!sec && sec !== 0) return "--";
  const s = Math.round(sec);
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
}

/** Inline spinner for busy buttons (currentColor so it fits any button). */
export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      className={`animate-spin ${className}`}
      aria-hidden="true"
    >
      <circle
        cx="12"
        cy="12"
        r="10"
        stroke="currentColor"
        strokeWidth="3"
        className="opacity-25"
      />
      <path
        d="M12 2a10 10 0 0 1 10 10"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  );
}

/** Shimmer placeholder block (loading states). */
export function Skeleton({ className = "h-4 w-full" }: { className?: string }) {
  return <div className={`skeleton rounded-lg ${className}`} aria-hidden="true" />;
}

/** Friendly empty state with an icon (instead of a bare grey sentence). */
export function EmptyState({
  title,
  hint,
  icon = "📭",
}: {
  title: string;
  hint?: string;
  icon?: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-1 py-8 text-center">
      <span className="text-3xl" aria-hidden="true">
        {icon}
      </span>
      <p className="text-sm font-semibold text-neutral-500 dark:text-neutral-400">
        {title}
      </p>
      {hint && (
        <p className="text-xs text-neutral-400 dark:text-neutral-500">{hint}</p>
      )}
    </div>
  );
}
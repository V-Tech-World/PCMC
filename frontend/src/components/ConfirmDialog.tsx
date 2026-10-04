import type { ReactNode } from "react";
import { Spinner } from "./ui";

/**
 * Confirmation modal. Every action that can spend money (real phone calls)
 * or run the scheduler goes through this -- an accidental click must never
 * place a call.
 */
export default function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "Confirm",
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  /** Red confirm button for money/spending actions (dials, live ticks). */
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  if (!open) return null;
  return (
    <div
      className="fade-in fixed inset-0 z-50 grid place-items-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={busy ? undefined : onCancel}
    >
      <div
        className="pop-in w-full max-w-md rounded-2xl bg-white p-5 shadow-2xl dark:bg-[#16203a]"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-bold text-ink dark:text-neutral-50">{title}</h3>
        <div className="mt-2 text-sm leading-relaxed text-neutral-600 dark:text-neutral-300">
          {message}
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="cursor-pointer rounded-lg px-4 py-2 text-sm font-semibold text-neutral-600 ring-1 ring-neutral-300 transition-all hover:bg-neutral-50 hover:ring-neutral-400 active:scale-95 dark:text-neutral-300 dark:ring-neutral-600 dark:hover:bg-white/5 dark:hover:ring-neutral-500"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className={`cursor-pointer inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-semibold text-white transition-all hover:shadow-md active:scale-95 disabled:opacity-50 ${
              danger
                ? "bg-red-600 hover:bg-red-700 dark:bg-red-700 dark:hover:bg-red-600"
                : "bg-brand-700 hover:bg-brand-800 dark:bg-brand-600 dark:hover:bg-brand-500"
            }`}
          >
            {busy && <Spinner className="h-3.5 w-3.5" />}
            {busy ? "Working…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
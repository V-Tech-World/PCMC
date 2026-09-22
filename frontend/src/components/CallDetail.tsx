/** Expanded call detail: transcript answers, findings, risk + actions. */
import type { CallRow } from "../lib/types";
import { RiskBadge, fmtDateTime } from "./ui";

export default function CallDetail({
  row,
  noteDraft,
  setNoteDraft,
  busy,
  canReview,
  canClose,
  onSaveNote,
  onMarkReviewed,
  onClose,
}: {
  row: CallRow;
  noteDraft: string;
  setNoteDraft: (v: string) => void;
  busy: boolean;
  canReview: boolean;
  canClose: boolean;
  onSaveNote: () => void;
  onMarkReviewed: () => void;
  onClose: () => void;
}) {
  const answers = row.answers ?? [];
  const findings = row.findings ?? [];
  const reasons = row.risk_reasons ?? [];

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
      {/* Transcript / answers */}
      <div className="lg:col-span-2 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <RiskBadge level={row.risk_level} score={row.risk_score} />
          <span className="text-xs text-neutral-500 dark:text-neutral-400">
            {fmtDateTime(row.started_at)} · {row.provider_call_id || "no provider id"}
            {row.closed_by && ` · closed by ${row.closed_by}`}
          </span>
        </div>

        {reasons.length > 0 && (
          <div className="rounded-lg bg-red-50 px-3 py-2 text-sm">
            <b className="text-red-800">Why this risk level:</b>
            <ul className="mt-1 list-inside list-disc text-red-700">
              {reasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          </div>
        )}

        {findings.length > 0 && (
          <div className="rounded-lg bg-amber-50 px-3 py-2 text-sm">
            <b className="text-amber-800">Symptoms found:</b>
            <ul className="mt-1 list-inside list-disc text-amber-700">
              {findings.map((f) => (
                <li key={f.id}>
                  {f.label} — {f.severity}
                  {f.red_flag ? " (red flag)" : ""}{" "}
                  <span className="text-amber-500">“{f.matched_text}”</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {answers.length === 0 ? (
          <p className="text-sm text-neutral-500 dark:text-neutral-400">
            No transcript stored for this call.
          </p>
        ) : (
          <ul className="space-y-2">
            {answers.map((a, i) => (
              <li
                key={`${a.question_id}-${i}`}
                className="rounded-lg bg-white px-3 py-2 text-sm ring-1 ring-neutral-200 dark:ring-neutral-700 dark:bg-white/5"
              >
                <span className="block text-xs font-bold uppercase tracking-wide text-neutral-400 dark:text-neutral-500">
                  {a.question_id} ·{" "}
                  {a.interpretation === true
                    ? "yes"
                    : a.interpretation === false
                      ? "no"
                      : String(a.interpretation ?? "")}
                </span>
                <span className="italic text-neutral-700 dark:text-neutral-200">
                  “{a.transcript}”
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Workflow actions */}
      <div className="space-y-3">
        <div className="rounded-lg bg-white px-3 py-2 text-sm ring-1 ring-neutral-200 dark:ring-neutral-700 dark:bg-white/5">
          <b>Alert:</b> {row.alert_status.replace("_", " ")}
          {row.alert_detail && (
            <span className="block text-xs text-neutral-400 dark:text-neutral-500">{row.alert_detail}</span>
          )}
        </div>

        <label className="block text-xs font-bold uppercase tracking-wide text-neutral-400 dark:text-neutral-500">
          Case note
          <textarea
            value={noteDraft}
            onChange={(e) => setNoteDraft(e.target.value)}
            rows={3}
            disabled={!canReview}
            placeholder={canReview ? "Short note for the team…" : "Read-only"}
            className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#121714] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200 disabled:bg-neutral-100 dark:bg-white/10"
          />
        </label>

        {canReview && (
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={onSaveNote}
              className="cursor-pointer rounded-lg bg-white px-3 py-2 text-xs font-bold text-neutral-700 dark:text-neutral-200 ring-1 ring-neutral-300 dark:ring-neutral-600 dark:bg-white/5 transition-all hover:bg-neutral-50 dark:hover:bg-white/5 hover:ring-neutral-400 active:scale-95 disabled:opacity-50"
            >
              Save note
            </button>
            <button
              type="button"
              disabled={busy || row.reviewed}
              onClick={onMarkReviewed}
              className="cursor-pointer rounded-lg bg-brand-700 px-3 py-2 text-xs font-bold text-white shadow-sm transition-all hover:bg-brand-800 hover:shadow active:scale-95 disabled:opacity-50 dark:bg-brand-600 dark:hover:bg-brand-500"
            >
              {row.reviewed ? "Reviewed ✓" : "Mark reviewed"}
            </button>
            {canClose && !row.closed_by && (
              <button
                type="button"
                disabled={busy}
                onClick={onClose}
                className="cursor-pointer rounded-lg bg-red-600 px-3 py-2 text-xs font-bold text-white shadow-sm transition-all hover:bg-red-700 hover:shadow active:scale-95 disabled:opacity-50 dark:bg-red-700 dark:hover:bg-red-600"
              >
                Close case
              </button>
            )}
          </div>
        )}
        {!canReview && (
          <p className="text-xs text-neutral-400 dark:text-neutral-500">
            Your role can view but not edit this case.
          </p>
        )}
      </div>
    </div>
  );
}
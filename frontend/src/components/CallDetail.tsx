/** Expanded call detail: transcript answers, findings, risk + actions. */
import { useState } from "react";
import { api, ApiError } from "../lib/api";
import type { CallRow } from "../lib/types";
import { RiskBadge, fmtDateTime } from "./ui";

export default function CallDetail({
  row,
  noteDraft,
  setNoteDraft,
  busy,
  canReview,
  canClose,
  canAlert,
  onSaveNote,
  onMarkReviewed,
  onClose,
  onAlertSent,
}: {
  row: CallRow;
  noteDraft: string;
  setNoteDraft: (v: string) => void;
  busy: boolean;
  canReview: boolean;
  canClose: boolean;
  canAlert: boolean;
  onSaveNote: () => void;
  onMarkReviewed: () => void;
  onClose: () => void;
  onAlertSent: (message: string, isError?: boolean) => void;
}) {
  const answers = row.answers ?? [];
  const findings = row.findings ?? [];
  const reasons = row.risk_reasons ?? [];
  const [copied, setCopied] = useState(false);
  const [sending, setSending] = useState(false);

  /** Re-run the alert for this HIGH-risk call (conversation was closed, the
   *  backend was still on the old config, delivery was off...). */
  async function sendAlert() {
    setSending(true);
    try {
      const res = await api.post<{ status: string; detail?: string }>(
        `/records/calls/${row.id}/alert`,
      );
      onAlertSent(
        res.status.startsWith("sent")
          ? `Alert sent (${res.status}).`
          : `Alert not sent: ${res.status}${res.detail ? ` — ${res.detail}` : ""}`,
        !res.status.startsWith("sent"),
      );
    } catch (err) {
      onAlertSent(
        err instanceof ApiError ? err.message : "Could not send the alert.",
        true,
      );
    } finally {
      setSending(false);
    }
  }

  // Step 7: the alert text is prepared for every HIGH-risk call and kept on the
  // row, so the nurse can hand it to the ward over whatever channel they use.
  async function copyAlert() {
    if (!row.alert_message) return;
    try {
      await navigator.clipboard.writeText(row.alert_message);
    } catch {
      // Clipboard blocked (insecure context / denied): select it instead so
      // Ctrl+C still works.
      const box = document.getElementById("alert-message-box");
      if (box) {
        const range = document.createRange();
        range.selectNodeContents(box);
        const sel = window.getSelection();
        sel?.removeAllRanges();
        sel?.addRange(range);
      }
      return;
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  }

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
          <div className="rounded-lg bg-red-50 px-3 py-2 text-sm dark:bg-red-950/40">
            <b className="text-red-800 dark:text-red-200">
              Why this risk level:
            </b>
            <ul className="mt-1 list-inside list-disc text-red-700 dark:text-red-300">
              {reasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          </div>
        )}

        {findings.length > 0 && (
          <div className="rounded-lg bg-amber-50 px-3 py-2 text-sm dark:bg-amber-950/40">
            <b className="text-amber-800 dark:text-amber-200">Symptoms found:</b>
            <ul className="mt-1 list-inside list-disc text-amber-700 dark:text-amber-300">
              {findings.map((f) => (
                <li key={f.id}>
                  {f.label} — {f.severity || "ungraded"}
                  {f.red_flag ? " (red flag)" : ""}{" "}
                  <span className="text-amber-600 dark:text-amber-400">
                    “{f.matched_text}”
                  </span>
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
        <div className="rounded-lg bg-white px-3 py-2 text-sm ring-1 ring-neutral-200 dark:bg-white/5 dark:ring-neutral-700">
          <b>Alert:</b> {row.alert_status.split(" (")[0].replace(/_/g, " ")}
          {/* The WhatsApp provider id lives in the backend log;
              show alert_detail only when it explains a failure. */}
          {row.alert_detail && !row.alert_detail.startsWith("provider message id") && (
            <span className="block text-xs text-neutral-500 dark:text-neutral-400">
              {row.alert_detail}
            </span>
          )}
        {/* 4 Oct 2026: who this call's alert actually reached (email, routed by
            score: below threshold nurses only, at/above nurses + doctors).
            The no_route placeholder means nobody could be reached -- show it in
            red, not silence: WhatsApp may say "sent" while email reached zero. */}
        {(row.alert_recipients ?? []).length > 0 && (
            <ul className="mt-1 space-y-1">
              {(row.alert_recipients ?? []).map((r, i) => (
                <li key={r.username || `note-${i}`} className="flex items-start justify-between gap-2 text-xs">
                  <span>
                    <span className="font-semibold">{r.display_name || r.username || "No routable staff"}</span>
                    {r.username && (
                      <span className="text-neutral-500 dark:text-neutral-400">
                        {" "}({r.role} · {r.email || "no email"})
                      </span>
                    )}
                    {r.status.startsWith("no_route") && (
                      <span className="block text-red-600 dark:text-red-400">
                        No active nurse/doctor account has a valid email — add one on the Staff screen.
                      </span>
                    )}
                  </span>
                  <span
                    className={
                      r.status === "sent"
                        ? "shrink-0 rounded-full bg-emerald-100 px-2 py-0.5 font-bold text-emerald-800 dark:bg-emerald-950/60 dark:text-emerald-200"
                        : "shrink-0 rounded-full bg-red-100 px-2 py-0.5 font-bold text-red-700 dark:bg-red-950/60 dark:text-red-300"
                    }
                    title={r.status}
                  >
                    {r.status === "sent" ? "sent ✓" : r.status.split(":")[0]}
                  </span>
                </li>
              ))}
            </ul>
          )}
          </div>

        {row.alert_message && (
          <div className="rounded-lg bg-amber-50 px-3 py-2 text-sm ring-1 ring-amber-200 dark:bg-amber-950/40 dark:ring-amber-800">
            <div className="flex items-center justify-between gap-2">
              <b className="text-amber-800 dark:text-amber-200">
                Alert message
                {row.alert_status === "ready" ? " (ready to send)" : ""}
              </b>
              <div className="flex shrink-0 gap-1.5">
                {canAlert && row.risk_level === "high" && (
                  <button
                    type="button"
                    onClick={() => void sendAlert()}
                    disabled={sending || busy}
                    className="cursor-pointer rounded-lg bg-brand-700 px-2 py-1 text-xs font-bold text-white transition-all hover:bg-brand-800 active:scale-95 disabled:opacity-50"
                  >
                    {sending ? "Sending…" : "Send now"}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => void copyAlert()}
                  className="shrink-0 cursor-pointer rounded-lg bg-white px-2 py-1 text-xs font-bold text-amber-800 ring-1 ring-amber-300 transition-all hover:bg-amber-100 active:scale-95 dark:bg-white/10 dark:text-amber-200 dark:ring-amber-700"
                >
                  {copied ? "Copied ✓" : "Copy"}
                </button>
              </div>
            </div>
            <pre
              id="alert-message-box"
              className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-white/70 p-2 font-sans text-xs text-neutral-800 dark:bg-black/30 dark:text-neutral-200"
            >
              {row.alert_message}
            </pre>
          </div>
        )}

        <label className="block text-xs font-bold uppercase tracking-wide text-neutral-400 dark:text-neutral-500">
          Case note
          <textarea
            value={noteDraft}
            onChange={(e) => setNoteDraft(e.target.value)}
            rows={3}
            disabled={!canReview}
            placeholder={canReview ? "Short note for the team…" : "Read-only"}
            className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#131c33] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200 disabled:bg-neutral-100 dark:bg-white/10"
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
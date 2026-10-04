import {
  Fragment,
  useCallback,
  useEffect,
  useState,
} from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import type { CallRow, RiskLevel } from "../lib/types";
import CallDetail from "../components/CallDetail";
import { useToast } from "../components/Toast";
import {
  Banner,
  Card,
  EmptyState,
  RiskBadge,
  Spinner,
  fmtDateTime,
  fmtDuration,
} from "../components/ui";

type Filter = "all" | RiskLevel | "open";

/** Step 7 states, as small dark-mode-safe chips. */
const ALERT_CHIP: { match: (s: string) => boolean; label: string; style: string }[] = [
  {
    match: (s) => s.startsWith("sent"),
    label: "Sent",
    style: "bg-emerald-100 text-emerald-700 ring-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-800",
  },
  {
    match: (s) => s === "ready",
    label: "Ready to send",
    style: "bg-amber-100 text-amber-800 ring-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:ring-amber-800",
  },
  {
    match: (s) => s.startsWith("failed") || s === "not_configured",
    label: "Not sent",
    style: "bg-red-100 text-red-700 ring-red-200 dark:bg-red-950 dark:text-red-300 dark:ring-red-800",
  },
];

function AlertChip({ status }: { status: string }) {
  const hit = ALERT_CHIP.find((c) => c.match(status));
  if (hit) {
    return (
      <span
        className={`inline-block rounded-full px-2 py-0.5 text-xs font-bold ring-1 ${hit.style}`}
      >
        {hit.label}
      </span>
    );
  }
  // skipped / not_sent: nothing to deliver, deliberately quiet.
  return (
    <span className="text-xs text-neutral-500 dark:text-neutral-400">
      {status.replace(/_/g, " ")}
    </span>
  );
}

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "open", label: "Open alerts" },
  { id: "high", label: "High" },
  { id: "medium", label: "Medium" },
  { id: "low", label: "Low" },
];

/**
 * Call list (Step 9 TC2): every row carries a risk-coloured badge; rows
 * expand into the full detail (answers, findings, risk reasons) with the
 * review / annotate / close-case workflow (role-gated).
 */
export default function CallsPage() {
  const { can } = useAuth();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const [rows, setRows] = useState<CallRow[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [noteDraft, setNoteDraft] = useState("");
  const [busy, setBusy] = useState(false);

  const filter: Filter = (params.get("filter") as Filter) || "all";

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const qs =
        filter === "open"
          ? "/records/calls?risk_level=high&reviewed=false&limit=200"
          : filter === "all"
            ? "/records/calls?limit=200"
            : `/records/calls?risk_level=${filter}&limit=200`;
      const data = await api.get<{ calls: CallRow[] }>(qs);
      setRows(data.calls);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load calls.");
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    void load();
  }, [load]);

  function setFilter(next: Filter) {
    setExpanded(null);
    setParams(next === "all" ? {} : { filter: next });
  }

  function toggle(row: CallRow) {
    if (expanded === row.id) {
      setExpanded(null);
    } else {
      setExpanded(row.id);
      setNoteDraft(row.nurse_note ?? "");
    }
  }

  async function patch(
    id: number,
    body: Record<string, unknown>,
    successMsg: string,
  ) {
    setBusy(true);
    try {
      await api.patch<{ status: string }>(`/records/calls/${id}`, body);
      toast.ok(successMsg);
      await load();
    } catch (err) {
      toast.error(
        err instanceof ApiError ? err.message : "Could not update the call.",
      );
    } finally {
      setBusy(false);
    }
  }

  const detail = rows.find((r) => r.id === expanded) ?? null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-extrabold text-ink dark:text-brand-50">Calls</h1>
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="Filter">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              type="button"
              onClick={() => setFilter(f.id)}
              className={`cursor-pointer rounded-full px-3 py-1.5 text-xs font-bold transition-all active:scale-95 ${
                filter === f.id
                  ? "bg-brand-700 text-white"
                  : "bg-white text-neutral-600 dark:text-neutral-300 ring-1 ring-neutral-300 dark:ring-neutral-600 dark:bg-white/5 hover:bg-brand-50 dark:hover:bg-white/5"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      {error && <Banner kind="error">{error}</Banner>}

      <Card
        title={`${rows.length} call(s)`}
        actions={
          <button
            type="button"
            onClick={() => void load()}
            className="inline-flex cursor-pointer items-center gap-1.5 text-xs font-bold text-brand-700 dark:text-brand-300 transition-colors hover:underline"
          >
            {loading && <Spinner className="h-3 w-3" />}
            Refresh
          </button>
        }
      >
        {loading ? (
          <span className="inline-flex items-center gap-2 text-sm text-neutral-500 dark:text-neutral-400">
            <Spinner /> Loading…
          </span>
        ) : rows.length === 0 ? (
          <EmptyState
            title="No calls match this filter"
            hint="Try another filter, or place a call from the Patients tab."
            icon="🔍"
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-neutral-200 dark:border-neutral-700 text-xs uppercase tracking-wide text-neutral-400 dark:text-neutral-500">
                  <th className="px-2 py-2">When</th>
                  <th className="px-2 py-2">Patient</th>
                  <th className="px-2 py-2">Phone</th>
                  <th className="px-2 py-2">Category</th>
                  <th className="px-2 py-2">Risk</th>
                  <th className="px-2 py-2">Duration</th>
                  <th className="px-2 py-2">Alert</th>
                  <th className="px-2 py-2">State</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <Fragment key={row.id}>
                    <tr
                      onClick={() => toggle(row)}
                      className={`cursor-pointer border-b border-neutral-100 dark:border-neutral-700/60 hover:bg-brand-50 dark:hover:bg-white/5 ${
                        // Expanded row: light tint in light mode, a subtle
                        // brand tint in dark mode (a plain bg-brand-50 here
                        // made the open row jump back to light mode).
                        expanded === row.id ? "bg-brand-50 dark:bg-brand-900/30" : ""
                      }`}
                    >
                      <td className="whitespace-nowrap px-2 py-2">
                        <span
                          className={`mr-1.5 inline-block text-neutral-400 dark:text-neutral-500 transition-transform duration-200 ${
                            expanded === row.id ? "rotate-90" : ""
                          }`}
                          aria-hidden="true"
                        >
                          ▸
                        </span>
                        {fmtDateTime(row.started_at)}
                      </td>
                      <td className="px-2 py-2 font-semibold">
                        {row.patient_code || "--"}
                      </td>
                      <td className="whitespace-nowrap px-2 py-2">
                        {row.phone_number}
                      </td>
                      <td className="px-2 py-2 capitalize">
                        {row.diagnosis_category}
                      </td>
                      <td className="px-2 py-2">
                        <RiskBadge level={row.risk_level} score={row.risk_score} />
                      </td>
                      <td className="whitespace-nowrap px-2 py-2">
                        {fmtDuration(row.duration_sec)}
                      </td>
                      <td className="px-2 py-2 text-xs">
                        <AlertChip status={row.alert_status} />
                          {/* 4 Oct 2026: who heard about it, at a glance. */}
                          {(row.alert_recipients ?? []).length > 0 && (
                            <span
                              className="mt-0.5 block text-neutral-500 dark:text-neutral-400"
                              title={(row.alert_recipients ?? [])
                                .map(
                                  (r) =>
                                    `${r.display_name || r.username} (${r.role}): ${r.status}`,
                                )
                                .join("\n")}
                            >
                              {(row.alert_recipients ?? []).some((r) =>
                            r.status.startsWith("no_route"),
                          ) ? (
                            <b className="text-red-600 dark:text-red-400">
                              ✉️ nobody emailed
                            </b>
                          ) : (
                            <>
                              ✉️ {(row.alert_recipients ?? []).filter((r) => r.status === "sent").length}/
                              {(row.alert_recipients ?? []).length} emailed
                            </>
                          )}
                            </span>
                          )}
                      </td>
                      <td className="px-2 py-2 text-xs">
                        {row.closed_by
                          ? `Closed by ${row.closed_by}`
                          : row.reviewed
                            ? "Reviewed"
                            : "Open"}
                      </td>
                    </tr>
                    {expanded === row.id && (
                      <tr className="border-b border-brand-100 dark:border-brand-900 bg-brand-50/40 dark:bg-brand-900/20">
                        <td colSpan={8} className="page-enter px-4 py-4">
                          <CallDetail
                            row={detail ?? row}
                            noteDraft={noteDraft}
                            setNoteDraft={setNoteDraft}
                            busy={busy}
                            canReview={can("review_call")}
                            canClose={can("close_case")}
                            canAlert={can("review_call")}
                            onSaveNote={() =>
                              void patch(row.id, { nurse_note: noteDraft }, "Case note saved.")
                            }
                            onMarkReviewed={() =>
                              void patch(
                                row.id,
                                { reviewed: true, nurse_note: noteDraft },
                                "Marked as reviewed ✓",
                              )
                            }
                            onClose={() =>
                              void patch(row.id, { close_case: true }, "Case closed ✓")
                            }
                            onAlertSent={(msg, isError) => {
                              if (isError) toast.error(msg);
                              else toast.ok(msg);
                              void load();
                            }}
                          />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
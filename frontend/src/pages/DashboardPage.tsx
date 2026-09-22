import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import type { DashboardSummary } from "../lib/types";
import {
  Banner,
  Card,
  EmptyState,
  RiskBadge,
  Spinner,
  StatCard,
  fmtDateTime,
  fmtDuration,
} from "../components/ui";

/** Stat-card icons (stroke, inherit color). */
const svgProps = {
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  className: "h-5 w-5",
  "aria-hidden": true,
};

const ICON_PHONE = (
  <svg {...svgProps}>
    <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z" />
  </svg>
);
const ICON_ALERT = (
  <svg {...svgProps}>
    <path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
  </svg>
);
const ICON_BELL = (
  <svg {...svgProps}>
    <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M13.7 21a2 2 0 0 1-3.4 0" />
  </svg>
);
const ICON_USERS = (
  <svg {...svgProps}>
    <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" />
  </svg>
);
const ICON_CALENDAR = (
  <svg {...svgProps}>
    <rect x="3" y="4" width="18" height="18" rx="2" />
    <path d="M16 2v4M8 2v4M3 10h18" />
  </svg>
);
const ICON_CLOCK = (
  <svg {...svgProps}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </svg>
);

/**
 * Landing screen: the six green cards from the sketch (real data from
 * GET /dashboard/summary) + recent activity table + open alerts + due
 * patients with a guarded Call-Now shortcut.
 */
export default function DashboardPage() {
  const { user, can } = useAuth();
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setSummary(await api.get<DashboardSummary>("/dashboard/summary"));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading && !summary) {
    return (
      <div className="space-y-6" aria-busy="true" aria-label="Loading dashboard">
        <div className="skeleton h-7 w-56" />
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="skeleton h-28 rounded-2xl" />
          ))}
        </div>
        <div className="skeleton h-64 rounded-xl" />
      </div>
    );
  }
  if (!summary) {
    return <Banner kind="error">{error || "No data."}</Banner>;
  }

  const c = summary.cards;
  const sched = summary.scheduler;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-extrabold text-ink dark:text-brand-50">
            Welcome back, {user?.display_name?.split(" ")[0] ?? "there"}
          </h1>
          <p className="mt-0.5 text-sm text-neutral-500 dark:text-neutral-400">
            {new Date().toLocaleDateString(undefined, {
              weekday: "long",
              month: "long",
              day: "numeric",
            })}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          className="inline-flex cursor-pointer items-center gap-2 rounded-lg px-3 py-1.5 text-sm font-semibold text-brand-700 dark:text-brand-300 ring-1 ring-brand-300 transition-all hover:bg-brand-50 dark:hover:bg-white/5 hover:ring-brand-400 active:scale-95"
        >
          {loading && <Spinner className="h-3.5 w-3.5" />}
          Refresh
        </button>
      </div>

      {error && <Banner kind="error">{error}</Banner>}

      {/* Six cards, 3 x 2 at desktop (the sketch's grid). */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <StatCard
          label="Total calls"
          value={c.calls_total}
          icon={ICON_PHONE}
          hint={c.last_call_at ? `Last: ${fmtDateTime(c.last_call_at)}` : "No calls yet"}
        />
        <StatCard
          label="High-risk calls"
          value={c.calls_by_risk.high ?? 0}
          icon={ICON_ALERT}
          hint={`Medium: ${c.calls_by_risk.medium ?? 0} · Low: ${c.calls_by_risk.low ?? 0}`}
        />
        <StatCard
          label="Open alerts"
          value={c.alerts_open}
          icon={ICON_BELL}
          hint={`${c.alerts_sent} WhatsApp alert(s) sent`}
        />
        <StatCard
          label="Active patients"
          value={c.patients_active}
          icon={ICON_USERS}
          hint={`${c.patients_total} registered`}
        />
        <StatCard
          label="Calls due"
          value={c.calls_due}
          icon={ICON_CALENDAR}
          hint={
            c.calls_due_total > c.calls_due
              ? `${c.calls_due_total - c.calls_due} outside grace window`
              : "Within the catch-up window"
          }
        />
        <StatCard
          label="Scheduler"
          value={sched.enabled ? "ON" : "OFF"}
          icon={ICON_CLOCK}
          hint={
            sched.enabled
              ? `Every ${sched.interval_minutes} min · next ${sched.next_tick_at ?? "--"}`
              : "Manual calls from this dashboard"
          }
        />
      </div>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <Card title="Recent calls">
            {summary.recent_calls.length === 0 ? (
              <EmptyState
                title="No calls yet"
                hint="Calls placed from the Patients tab will appear here."
                icon="📞"
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead>
                    <tr className="border-b border-neutral-200 dark:border-neutral-700 text-xs uppercase tracking-wide text-neutral-400 dark:text-neutral-500">
                      <th className="px-2 py-2">When</th>
                      <th className="px-2 py-2">Patient</th>
                      <th className="px-2 py-2">Category</th>
                      <th className="px-2 py-2">Risk</th>
                      <th className="px-2 py-2">Duration</th>
                      <th className="px-2 py-2">Alert</th>
                      <th className="px-2 py-2">State</th>
                    </tr>
                  </thead>
                  <tbody>
                    {summary.recent_calls.map((row) => (
                      <tr
                        key={row.id}
                        className="border-b border-neutral-100 dark:border-neutral-700/60 hover:bg-brand-50 dark:hover:bg-white/5/50 dark:hover:bg-white/5"
                      >
                        <td className="whitespace-nowrap px-2 py-2">
                          {fmtDateTime(row.started_at)}
                        </td>
                        <td className="px-2 py-2 font-semibold">
                          {row.patient_code || "--"}
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
                        <td className="px-2 py-2 text-xs capitalize">
                          {row.alert_status.replace("_", " ")}
                        </td>
                        <td className="px-2 py-2 text-xs">
                          {row.closed_by
                            ? `Closed by ${row.closed_by}`
                            : row.reviewed
                              ? "Reviewed"
                              : "Open"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="mt-3">
              <Link
                to="/calls"
                className="text-sm font-semibold text-brand-700 dark:text-brand-300 hover:underline"
              >
                Open all calls →
              </Link>
            </div>
          </Card>
        </div>

        <div className="space-y-6">
          <Card title={`Open alerts (${summary.open_alerts.length})`}>
            {summary.open_alerts.length === 0 ? (
              <EmptyState
                title="Nothing waiting for review"
                icon="✅"
              />
            ) : (
              <ul className="space-y-2">
                {summary.open_alerts.map((row) => (
                  <li
                    key={row.id}
                    className="flex items-center justify-between gap-2 rounded-lg bg-red-50 px-3 py-2"
                  >
                    <span className="text-sm font-semibold text-red-800">
                      {row.patient_code || row.phone_number}
                    </span>
                    <RiskBadge level={row.risk_level} score={row.risk_score} />
                  </li>
                ))}
              </ul>
            )}
            <div className="mt-3">
              <Link
                to="/calls?filter=open"
                className="text-sm font-semibold text-brand-700 dark:text-brand-300 hover:underline"
              >
                Review alerts →
              </Link>
            </div>
          </Card>

          <Card title={`Due today (${summary.due_patients.length})`}>
            {summary.due_patients.length === 0 ? (
              <EmptyState
                title="No check-in due right now"
                icon="🗓️"
              />
            ) : (
              <ul className="space-y-2">
                {summary.due_patients.map((row) => (
                  <li
                    key={row.patient_code}
                    className="flex items-center justify-between gap-2 rounded-lg bg-brand-50 px-3 py-2 dark:bg-white/5"
                  >
                    <span className="text-sm">
                      <b>{row.patient_code}</b> · {row.name}
                      <span className="block text-xs text-neutral-500 dark:text-neutral-400">
                        {row.next_call_at ?? ""}{" "}
                        {row.days_overdue > 0
                          ? `· ${row.days_overdue}d overdue`
                          : ""}
                      </span>
                    </span>
                    {can("call_patient") && (
                      <Link
                        to={`/patients?call=${encodeURIComponent(row.patient_code)}`}
                        className="cursor-pointer rounded-lg bg-brand-700 px-3 py-1.5 text-xs font-bold text-white shadow-sm transition-all hover:bg-brand-800 hover:shadow active:scale-95"
                      >
                        Call
                      </Link>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}
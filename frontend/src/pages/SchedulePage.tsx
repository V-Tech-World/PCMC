import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import type {
  RunNowResponse,
  ScheduleBoard,
  SchedulerStatus,
} from "../lib/types";
import ConfirmDialog from "../components/ConfirmDialog";
import { useToast } from "../components/Toast";
import { Banner, Card, EmptyState, Spinner, fmtDate } from "../components/ui";

const STATUS_STYLES: Record<string, string> = {
  due: "bg-amber-100 text-amber-800 ring-amber-200 dark:bg-amber-950 dark:text-amber-200 dark:ring-amber-800",
  overdue:
    "bg-red-100 text-red-700 ring-red-200 dark:bg-red-950 dark:text-red-300 dark:ring-red-800",
  scheduled:
    "bg-brand-100 text-brand-800 ring-brand-200 dark:bg-[#122347] dark:text-brand-200 dark:ring-brand-700",
  completed:
    "bg-emerald-100 text-emerald-700 ring-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-800",
};

/**
 * Schedule screen (Step 8, TC3): every patient's full check-in timeline, the
 * master-switch banner, and an admin toggle that turns the automatic dialer
 * on/off (POST /schedule/enabled -- persisted, restart-safe). While the switch
 * is OFF the table still shows the REAL next-call times and "Run one tick now"
 * only reports the plan (it can never dial while the switch is off).
 */
export default function SchedulePage() {
  const { can } = useAuth();
  const toast = useToast();
  const [board, setBoard] = useState<ScheduleBoard | null>(null);
  const [status, setStatus] = useState<SchedulerStatus | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [tickBusy, setTickBusy] = useState(false);
  const [tickResult, setTickResult] = useState<RunNowResponse | null>(null);
  const [arming, setArming] = useState(false); // confirm dialog: turn ON
  const [switchBusy, setSwitchBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [b, s] = await Promise.all([
        api.get<ScheduleBoard>("/schedule"),
        api.get<SchedulerStatus>("/schedule/status"),
      ]);
      setBoard(b);
      setStatus(s);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load schedule.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function runTick() {
    setTickBusy(true);
    setError("");
    try {
      setTickResult(await api.post<RunNowResponse>("/schedule/run-now", {}));
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Tick failed.");
    } finally {
      setTickBusy(false);
      setConfirmOpen(false);
      await load();
    }
  }

  /** Flip the automatic dialer (admin, POST /schedule/enabled).
   * Turning it OFF applies immediately (never dangerous); turning it ON goes
   * through the arming confirm dialog first because the cron may then dial
   * due patients on its next tick -- through the usual cost rails. */
  async function setSwitch(next: boolean) {
    setSwitchBusy(true);
    try {
      const s = await api.post<SchedulerStatus>("/schedule/enabled", {
        enabled: next,
      });
      setStatus(s);
      toast.ok(
        s.message ??
          `Automatic follow-up calls turned ${next ? "ON" : "OFF"} (saved).`,
      );
    } catch (err) {
      toast.error(
        err instanceof ApiError ? err.message : "Could not change the switch.",
      );
    } finally {
      setSwitchBusy(false);
      setArming(false);
      await load();
    }
  }

  if (loading && !board) {
    return (
      <span className="inline-flex items-center gap-2 text-sm text-neutral-500 dark:text-neutral-400">
        <Spinner /> Loading schedule…
      </span>
    );
  }
  if (!board || !status) {
    return <Banner kind="error">{error || "No data."}</Banner>;
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-extrabold text-ink dark:text-brand-50">Follow-up schedule</h1>

      {error && <Banner kind="error">{error}</Banner>}

      {/* Master switch banner */}
      <div
        className={`rounded-xl px-4 py-3 text-sm font-medium ring-1 ${
          status.enabled
            ? "bg-amber-50 text-amber-800 ring-amber-200 dark:bg-amber-950 dark:text-amber-200 dark:ring-amber-800"
            : "bg-brand-50 text-brand-800 ring-brand-200 dark:bg-[#122347] dark:text-brand-200 dark:ring-brand-700"
        }`}
      >
        <b className="inline-flex items-center gap-2">
          <span
            className={`inline-block h-2.5 w-2.5 rounded-full ${
              status.enabled ? "animate-pulse bg-red-500" : "bg-emerald-500"
            }`}
            aria-hidden="true"
          />
          Automatic calls: {status.enabled ? "ON" : "OFF"}
        </b>{" "}
        {status.note}
        <span className="mt-1 block text-xs opacity-75">
          Check-in offsets: {status.checkin_offsets_days.join(", ")} days after
          discharge · due at {status.slot_local_time} · grace {status.grace_days}{" "}
          day(s) · max {status.max_dials_per_tick} dial(s) per tick · cooldown{" "}
          {status.min_hours_between_calls}h between auto-calls per patient · tick
          every {status.interval_minutes} min
          {status.next_tick_at && ` · next tick ${status.next_tick_at}`}
        </span>

        {/* Master on/off switch (admin): OFF is one click, ON confirms first. */}
        {can("manage_scheduler") && (
          <div className="mt-3 flex items-center gap-3 border-t border-black/10 pt-3 dark:border-white/10">
            <button
              type="button"
              role="switch"
              aria-checked={status.enabled}
              aria-label="Automatic follow-up calls"
              disabled={switchBusy}
              onClick={() => (status.enabled ? void setSwitch(false) : setArming(true))}
              className={`relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-60 ${
                status.enabled
                  ? "bg-red-500 hover:bg-red-600"
                  : "bg-neutral-300 hover:bg-neutral-400 dark:bg-neutral-600 dark:hover:bg-neutral-500"
              }`}
            >
              <span
                className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow transition-all ${
                  status.enabled ? "left-[22px]" : "left-0.5"
                }`}
              />
            </button>
            <span className="text-xs font-bold uppercase tracking-wide">
              {switchBusy
                ? "Saving…"
                : status.enabled
                  ? "Auto-dial active"
                  : "Auto-dial inactive"}
            </span>
          </div>
        )}
      </div>

      {tickResult && (
        <Banner kind={tickResult.dialed > 0 ? "info" : "ok"}>
          Tick at {tickResult.ran_at}: {tickResult.planned_count} planned,{" "}
          {tickResult.dialed} dialed, {tickResult.skipped} skipped.{" "}
          {tickResult.message}
        </Banner>
      )}

      <Card
        title={`${board.count} patient(s) · ${board.due_count} due now`}
        actions={
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => void load()}
              className="inline-flex cursor-pointer items-center gap-1.5 text-xs font-bold text-brand-700 dark:text-brand-300 transition-colors hover:underline"
            >
              {loading && <Spinner className="h-3 w-3" />}
              Refresh
            </button>
            {can("manage_scheduler") && (
              <button
                type="button"
                onClick={() => setConfirmOpen(true)}
                className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg bg-brand-700 px-3 py-1.5 text-xs font-bold text-white shadow-sm transition-all hover:bg-brand-800 hover:shadow active:scale-95"
              >
                Run one tick now
              </button>
            )}
          </div>
        }
      >
        {board.patients.length === 0 ? (
          <EmptyState
            title="No patients registered yet"
            hint="An admin can add them from the Patients tab."
            icon="🧑‍⚕️"
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-neutral-200 dark:border-neutral-700 text-xs uppercase tracking-wide text-neutral-400 dark:text-neutral-500">
                  <th className="px-2 py-2">Patient</th>
                  <th className="px-2 py-2">Discharged</th>
                  <th className="px-2 py-2">Next check-in</th>
                  <th className="px-2 py-2">Status</th>
                  <th className="px-2 py-2">Auto-dial?</th>
                </tr>
              </thead>
              <tbody>
                {board.patients.map((row) => (
                  <tr
                    key={row.patient_code}
                    className="border-b border-neutral-100 dark:border-neutral-700/60 hover:bg-brand-50 dark:hover:bg-white/5"
                  >
                    <td className="px-2 py-2">
                      <b>{row.patient_code}</b>
                      <span className="block text-xs text-neutral-500 dark:text-neutral-400">
                        {row.name} · {row.phone_number}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-2 py-2">
                      {fmtDate(row.discharge_date) || "--"}
                    </td>
                    <td className="whitespace-nowrap px-2 py-2">
                      {row.next_call_at ?? "--"}
                      {row.days_overdue > 0 && (
                        <span className="ml-1 text-xs font-bold text-red-600 dark:text-red-400">
                          {row.days_overdue}d overdue
                        </span>
                      )}
                      {(row.cooldown_hours ?? 0) > 0 && (
                        <span className="ml-1 text-xs font-semibold text-neutral-500 dark:text-neutral-400">
                          next auto-dial in {row.cooldown_hours}h
                        </span>
                      )}
                    </td>
                    <td className="px-2 py-2">
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs font-bold capitalize ring-1 ${
                          STATUS_STYLES[row.status] ??
                          "bg-slate-100 text-slate-600 ring-slate-200 dark:bg-neutral-800 dark:text-neutral-300 dark:ring-neutral-700"
                        }`}
                      >
                        {row.status.replace("_", " ")}
                      </span>
                    </td>
                    <td className="px-2 py-2 text-xs">
                      {row.will_dial_automatically ? "Yes" : "No — manual"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <ConfirmDialog
        open={confirmOpen}
        title={status.enabled ? "Run the scheduler tick now?" : "Run a dry tick?"}
        message={
          status.enabled ? (
            <>
              Automatic calls are <b>ON</b>: this tick will place up to{" "}
              {status.max_dials_per_tick} real phone call(s) to patients whose
              check-in is due (inside the {status.grace_days}-day grace window).
            </>
          ) : (
            <>
              Automatic calls are <b>OFF</b>: this only reports the dial
              plan — <b>no call will be placed</b>.
            </>
          )
        }
        confirmLabel={status.enabled ? "Run tick (may dial)" : "Show the plan"}
        danger={status.enabled}
        busy={tickBusy}
        onConfirm={() => void runTick()}
        onCancel={() => setConfirmOpen(false)}
      />

      <ConfirmDialog
        open={arming}
        title="Turn automatic calls ON?"
        message={
          <>
            The cronjob will start dialing patients whose check-in is due — up
            to {status.max_dials_per_tick} call(s) every{" "}
            {status.interval_minutes} min (and never the same patient twice
            within {status.min_hours_between_calls}h), through the same cost
            rails as manual calls. <b>No call is placed by this switch itself</b>; the
            choice is saved and survives a backend restart.
          </>
        }
        confirmLabel="Turn on auto-dialing"
        danger
        busy={switchBusy}
        onConfirm={() => void setSwitch(true)}
        onCancel={() => setArming(false)}
      />
    </div>
  );
}
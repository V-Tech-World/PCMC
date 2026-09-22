import { useCallback, useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import type { DialResponse, Patient, ScheduleBoard } from "../lib/types";
import ConfirmDialog from "../components/ConfirmDialog";
import { useToast } from "../components/Toast";
import { Banner, Card, EmptyState, Spinner, fmtDate } from "../components/ui";

const CATEGORIES = ["general", "surgical", "cardiac"] as const;

const EMPTY_FORM = {
  patient_code: "",
  name: "",
  phone_number: "",
  diagnosis_category: "general",
  discharge_date: "",
  notes: "",
};

/**
 * Patient register (admin-managed) + the manual "Call Now" button (TC3).
 * Dialing costs real money, so every call goes through a confirm dialog
 * that names the number being dialed.
 */
export default function PatientsPage() {
  const { can } = useAuth();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const [patients, setPatients] = useState<Patient[]>([]);
  const [nextCalls, setNextCalls] = useState<Record<string, string | null>>({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [formBusy, setFormBusy] = useState(false);

  const [dialTarget, setDialTarget] = useState<Patient | null>(null);
  const [dialBusy, setDialBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [p, s] = await Promise.all([
        api.get<{ patients: Patient[] }>("/records/patients"),
        api.get<ScheduleBoard>("/schedule"),
      ]);
      setPatients(p.patients);
      const map: Record<string, string | null> = {};
      for (const row of s.patients) map[row.patient_code] = row.next_call_at;
      setNextCalls(map);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load patients.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Deep link from the dashboard's "Due today" list: ?call=P-0001 preopens
  // the confirm dialog for that patient (the human still confirms the dial).
  useEffect(() => {
    const code = params.get("call");
    if (!code) return;
    const patient = patients.find((p) => p.patient_code === code);
    if (patient && can("call_patient")) setDialTarget(patient);
    params.delete("call");
    setParams(params, { replace: true });
  }, [params, patients, can, setParams]);

  async function savePatient(event: FormEvent) {
    event.preventDefault();
    setFormBusy(true);
    setError("");
    try {
      const res = await api.post<{ status: string }>("/records/patients", form);
      toast.ok(
        res.status === "created"
          ? `Patient ${form.patient_code} created.`
          : `Patient ${form.patient_code} updated.`,
      );
      setForm(EMPTY_FORM);
      setShowForm(false);
      await load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not save.");
    } finally {
      setFormBusy(false);
    }
  }

  async function confirmDial() {
    if (!dialTarget) return;
    setDialBusy(true);
    setError("");
    try {
      const res = await api.post<DialResponse>("/calls", {
        patient_code: dialTarget.patient_code,
        diagnosis_category: dialTarget.diagnosis_category,
      });
      toast.ok(
        `Dialing ${res.to} — provider call ${res.provider_call_id} (${res.provider_status}).`,
      );
      setDialTarget(null);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Dial failed.");
      setDialTarget(null);
    } finally {
      setDialBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-extrabold text-ink dark:text-brand-50">Patients</h1>
        {can("manage_patients") && (
          <button
            type="button"
            onClick={() => setShowForm((v) => !v)}
            className="cursor-pointer rounded-lg bg-brand-700 px-4 py-2 text-sm font-bold text-white shadow-sm transition-all hover:bg-brand-800 hover:shadow active:scale-95"
          >
            {showForm ? "Close form" : "+ Add patient"}
          </button>
        )}
      </div>

      {error && <Banner kind="error">{error}</Banner>}

      {showForm && can("manage_patients") && (
        <Card title="Register / update patient">
          <form onSubmit={savePatient} className="grid gap-3 sm:grid-cols-2">
            <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
              Patient code *
              <input
                required
                value={form.patient_code}
                onChange={(e) => setForm({ ...form, patient_code: e.target.value })}
                placeholder="P-0001"
                className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#121714] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
              />
            </label>
            <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
              Name
              <input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#121714] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
              />
            </label>
            <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
              Phone (E.164) *
              <input
                required
                value={form.phone_number}
                onChange={(e) => setForm({ ...form, phone_number: e.target.value })}
                placeholder="+94771234567"
                className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#121714] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
              />
            </label>
            <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
              Category
              <select
                value={form.diagnosis_category}
                onChange={(e) =>
                  setForm({ ...form, diagnosis_category: e.target.value })
                }
                className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#121714] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
              >
                {CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
              Discharge date
              <input
                type="date"
                value={form.discharge_date}
                onChange={(e) => setForm({ ...form, discharge_date: e.target.value })}
                className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#121714] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
              />
            </label>
            <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300 sm:col-span-2">
              Notes
              <textarea
                rows={2}
                value={form.notes}
                onChange={(e) => setForm({ ...form, notes: e.target.value })}
                className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#121714] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
              />
            </label>
            <div className="sm:col-span-2">
              <button
                type="submit"
                disabled={formBusy}
                className="inline-flex cursor-pointer items-center gap-2 rounded-lg bg-brand-700 px-4 py-2 text-sm font-bold text-white shadow-sm transition-all hover:bg-brand-800 hover:shadow active:scale-95 disabled:opacity-50"
              >
                {formBusy && <Spinner className="h-4 w-4" />}
                {formBusy ? "Saving…" : "Save patient"}
              </button>
            </div>
          </form>
        </Card>
      )}

      <Card title={`${patients.length} patient(s)`}>
        {loading ? (
          <span className="inline-flex items-center gap-2 text-sm text-neutral-500 dark:text-neutral-400">
            <Spinner /> Loading…
          </span>
        ) : patients.length === 0 ? (
          <EmptyState
            title="No patients yet"
            hint={
              can("manage_patients")
                ? "Use + Add patient to register one."
                : "Ask an administrator to register patients."
            }
            icon="🧑‍⚕️"
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-neutral-200 dark:border-neutral-700 text-xs uppercase tracking-wide text-neutral-400 dark:text-neutral-500">
                  <th className="px-2 py-2">Code</th>
                  <th className="px-2 py-2">Name</th>
                  <th className="px-2 py-2">Phone</th>
                  <th className="px-2 py-2">Category</th>
                  <th className="px-2 py-2">Discharged</th>
                  <th className="px-2 py-2">Next check-in</th>
                  <th className="px-2 py-2">Status</th>
                  <th className="px-2 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {patients.map((p) => (
                  <tr
                    key={p.patient_code}
                    className="border-b border-neutral-100 dark:border-neutral-700/60 hover:bg-brand-50 dark:hover:bg-white/5/50 dark:hover:bg-white/5"
                  >
                    <td className="px-2 py-2 font-semibold">{p.patient_code}</td>
                    <td className="px-2 py-2">{p.name || "--"}</td>
                    <td className="whitespace-nowrap px-2 py-2">{p.phone_number}</td>
                    <td className="px-2 py-2 capitalize">{p.diagnosis_category}</td>
                    <td className="whitespace-nowrap px-2 py-2">
                      {fmtDate(p.discharge_date) || "--"}
                    </td>
                    <td className="whitespace-nowrap px-2 py-2 text-xs">
                      {nextCalls[p.patient_code] ?? "--"}
                    </td>
                    <td className="px-2 py-2 text-xs">
                      {p.active ? (
                        <span className="rounded-full bg-emerald-100 px-2 py-0.5 font-bold text-emerald-700">
                          active
                        </span>
                      ) : (
                        <span className="rounded-full bg-slate-100 px-2 py-0.5 font-bold text-slate-500">
                          inactive
                        </span>
                      )}
                    </td>
                    <td className="px-2 py-2 text-right">
                      {can("call_patient") && p.active && (
                        <button
                          type="button"
                          onClick={() => setDialTarget(p)}
                          className="cursor-pointer rounded-lg bg-brand-700 px-3 py-1.5 text-xs font-bold text-white shadow-sm transition-all hover:bg-brand-800 hover:shadow active:scale-95"
                        >
                          ☎ Call now
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <ConfirmDialog
        open={dialTarget !== null}
        title="Place a real phone call?"
        message={
          dialTarget ? (
            <>
              This dials <b>{dialTarget.phone_number}</b> (
              {dialTarget.patient_code}, {dialTarget.diagnosis_category}) through
              Zernio. Every answered minute is billed and counts against the
              hourly/daily call limits. The patient will hear the automated
              check-in conversation.
            </>
          ) : (
            ""
          )
        }
        confirmLabel="Place the call"
        danger
        busy={dialBusy}
        onConfirm={() => void confirmDial()}
        onCancel={() => setDialTarget(null)}
      />
    </div>
  );
}
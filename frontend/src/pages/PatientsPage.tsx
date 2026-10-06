import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import type {
  DialResponse,
  Patient,
  ScheduleBoard,
  StaffUser,
} from "../lib/types";
import ConfirmDialog from "../components/ConfirmDialog";
import { useToast } from "../components/Toast";
import { Banner, Card, EmptyState, Spinner, fmtDate } from "../components/ui";

/**
 * Discharge types. The backend validates against app/services/dialogue.CATEGORIES
 * (general | surgical | cardiac | respiratory | diabetic), so this list must
 * stay in step with it.
 */
const CATEGORIES = [
  "general",
  "surgical",
  "cardiac",
  "respiratory",
  "diabetic",
] as const;

const LANGUAGES = [
  { value: "en", label: "English" },
  { value: "ta", label: "Tamil" },
  { value: "si", label: "Sinhala" },
] as const;

const EMPTY_FORM = {
  patient_code: "",
  name: "",
  phone_number: "",
  diagnosis_category: "general",
  discharge_date: "",
  notes: "",
  language_pref: "en",
  active: true,
  /** Staff usernames: the nurse(s) + doctor(s) emailed on HIGH risk. */
  assigned_staff: [] as string[],
};

/**
 * Patient register (admin-managed), the edit action for an existing record,
 * and the manual "Call Now" button (TC3).
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
  /** patient_code being edited, or null when the form creates a new record. */
  const [editing, setEditing] = useState<string | null>(null);
  const formRef = useRef<HTMLFormElement | null>(null);

  const [dialTarget, setDialTarget] = useState<Patient | null>(null);
  const [dialBusy, setDialBusy] = useState(false);

  /**
   * Nurses + doctors available for assignment. Loaded alongside the
   * patients (admin-only endpoint -- a nurse sees an empty list and a hint
   * instead of a 403). The backend re-validates every save, so a stale row
   * here can never silently drop someone from the alerts.
   */
  const [careTeam, setCareTeam] = useState<StaffUser[]>([]);

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
      try {
        const staff = await api.get<{ staff: StaffUser[] }>("/auth/staff");
        setCareTeam(
          staff.staff.filter(
            (u) => u.active && (u.role === "nurse" || u.role === "doctor"),
          ),
        );
      } catch {
        // Non-admins cannot list staff: leave the picker empty (the create /
        // edit form is admin-gated anyway) rather than failing the page.
        setCareTeam([]);
      }
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

  /** Open the form pre-filled on an existing record (admin only). */
  function startEdit(p: Patient) {
    setForm({
      patient_code: p.patient_code,
      name: p.name ?? "",
      phone_number: p.phone_number ?? "",
      diagnosis_category: p.diagnosis_category || "general",
      discharge_date: p.discharge_date ?? "",
      notes: p.notes ?? "",
      language_pref: p.language_pref || "en",
      active: p.active ?? true,
      /** Edit uses the same form: what is saved here is who gets emailed. */
      assigned_staff: [...(p.assigned_staff ?? [])],
    });
    setEditing(p.patient_code);
    setShowForm(true);
    setError("");
    // Bring the form into view instead of leaving the user at the table row.
    window.requestAnimationFrame(() =>
      formRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }),
    );
  }

  function closeForm() {
    setShowForm(false);
    setForm(EMPTY_FORM);
    setEditing(null);
  }

  async function savePatient(event: FormEvent) {
    event.preventDefault();
    setFormBusy(true);
    setError("");
    try {
      // Same endpoint for create and edit: the backend matches on
      // patient_code, so an edit updates the record in place.
      const res = await api.post<{ status: string }>("/records/patients", form);
      toast.ok(
        res.status === "created"
          ? `Patient ${form.patient_code} created.`
          : `Patient ${form.patient_code} updated.`,
      );
      closeForm();
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
            onClick={() => (showForm ? closeForm() : setShowForm(true))}
            className="cursor-pointer rounded-lg bg-brand-700 px-4 py-2 text-sm font-bold text-white shadow-sm transition-all hover:bg-brand-800 hover:shadow active:scale-95"
          >
            {showForm ? "Close form" : "+ Add patient"}
          </button>
        )}
      </div>

      {error && <Banner kind="error">{error}</Banner>}

      {showForm && can("manage_patients") && (
        <Card
          title={editing ? `Edit patient ${editing}` : "Register a patient"}
        >
          <form
            ref={formRef}
            onSubmit={savePatient}
            className="grid gap-3 sm:grid-cols-2"
          >
            <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
              Patient code *
              <input
                required
                readOnly={editing !== null}
                value={form.patient_code}
                onChange={(e) => setForm({ ...form, patient_code: e.target.value })}
                placeholder="P-0001"
                className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#131c33] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200 read-only:bg-neutral-100 read-only:text-neutral-600 dark:read-only:bg-white/5 dark:read-only:text-neutral-400"
              />
              {editing !== null && (
                <span className="mt-1 block text-xs font-normal text-neutral-500 dark:text-neutral-400">
                  The code is the record key, so it stays as it is. Everything
                  else can be corrected.
                </span>
              )}
            </label>
            <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
              Name
              <input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#131c33] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
              />
            </label>
            <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
              Phone (E.164) *
              <input
                required
                value={form.phone_number}
                onChange={(e) => setForm({ ...form, phone_number: e.target.value })}
                placeholder="+94771234567"
                className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#131c33] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
              />
            </label>
            <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
              Discharge type
              <select
                value={form.diagnosis_category}
                onChange={(e) =>
                  setForm({ ...form, diagnosis_category: e.target.value })
                }
                className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#131c33] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
              >
                {CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
              <span className="mt-1 block text-xs font-normal text-neutral-500 dark:text-neutral-400">
                Picks the one type-specific question asked on the call.
              </span>
            </label>
            <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
              Discharge date
              <input
                type="date"
                value={form.discharge_date}
                onChange={(e) => setForm({ ...form, discharge_date: e.target.value })}
                className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#131c33] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
              />
            </label>
            <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
              Preferred language
              <select
                value={form.language_pref}
                onChange={(e) =>
                  setForm({ ...form, language_pref: e.target.value })
                }
                className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#131c33] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
              >
                {LANGUAGES.map((l) => (
                  <option key={l.value} value={l.value}>
                    {l.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2 self-end rounded-lg border border-neutral-300 px-3 py-2 text-sm font-semibold text-neutral-600 dark:border-neutral-600 dark:text-neutral-300 sm:col-span-2 dark:bg-white/5">
              <input
                type="checkbox"
                checked={form.active}
                onChange={(e) => setForm({ ...form, active: e.target.checked })}
                className="h-4 w-4 accent-brand-700"
              />
              Active (uncheck for a discharged/archived patient)
            </label>
            {/*
             * Care team: the nurse(s) + doctor(s) emailed on HIGH risk.
             * Same control on create AND edit (the form is shared): the
             * backend re-validates every save, so a typo fails here with
             * the reason instead of silently dropping an alert at 3am.
             */}
            <fieldset className="rounded-lg border border-neutral-300 px-3 py-2 text-sm font-semibold text-neutral-600 dark:border-neutral-600 dark:text-neutral-300 sm:col-span-2 dark:bg-white/5">
              <legend className="px-1">
                Care team — emailed on HIGH risk
              </legend>
              {careTeam.length === 0 ? (
                <p className="py-1 text-xs font-normal text-neutral-500 dark:text-neutral-400">
                  No nurses or doctors available — create them on the Staff
                  screen first. Saving without a team means nobody is emailed
                  (the dashboard will say so on the call).
                </p>
              ) : (
                <div className="grid gap-1.5 py-1 sm:grid-cols-2">
                  {careTeam.map((u) => {
                    const checked = form.assigned_staff.includes(u.username);
                    return (
                      <label
                        key={u.username}
                        className="flex cursor-pointer items-center gap-2 font-normal text-neutral-700 dark:text-neutral-200"
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={(e) =>
                            setForm({
                              ...form,
                              assigned_staff: e.target.checked
                                ? [...form.assigned_staff, u.username]
                                : form.assigned_staff.filter(
                                    (name) => name !== u.username,
                                  ),
                            })
                          }
                          className="h-4 w-4 accent-brand-700"
                        />
                        <span className="font-semibold">{u.username}</span>
                        <span className="rounded-full bg-brand-100 px-2 py-0.5 text-[11px] font-bold uppercase text-brand-800 dark:bg-[#122347] dark:text-brand-200">
                          {u.role}
                        </span>
                      </label>
                    );
                  })}
                </div>
              )}
              {form.assigned_staff.length === 0 && (
                <p className="pb-1 text-xs font-semibold text-amber-600 dark:text-amber-400">
                  Nobody selected — HIGH-risk calls for this patient will email
                  nobody (WhatsApp still sends).
                </p>
              )}
            </fieldset>
            <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300 sm:col-span-2">
              Notes
              <textarea
                rows={2}
                value={form.notes}
                onChange={(e) => setForm({ ...form, notes: e.target.value })}
                className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#131c33] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
              />
            </label>
            <div className="flex flex-wrap items-center gap-2 sm:col-span-2">
              <button
                type="submit"
                disabled={formBusy}
                className="inline-flex cursor-pointer items-center gap-2 rounded-lg bg-brand-700 px-4 py-2 text-sm font-bold text-white shadow-sm transition-all hover:bg-brand-800 hover:shadow active:scale-95 disabled:opacity-50"
              >
                {formBusy && <Spinner className="h-4 w-4" />}
                {formBusy
                  ? "Saving…"
                  : editing
                    ? "Save changes"
                    : "Save patient"}
              </button>
              <button
                type="button"
                onClick={closeForm}
                className="cursor-pointer rounded-lg bg-white px-4 py-2 text-sm font-bold text-neutral-700 ring-1 ring-neutral-300 transition-all hover:bg-neutral-50 active:scale-95 dark:bg-white/5 dark:text-neutral-200 dark:ring-neutral-600 dark:hover:bg-white/10"
              >
                Cancel
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
                  <th className="px-2 py-2">Care team</th>
                  <th className="px-2 py-2">Status</th>
                  <th className="px-2 py-2 text-right">Actions</th>
                </tr>
              </thead>
              <tbody>
                {patients.map((p) => (
                  <tr
                    key={p.patient_code}
                    className="border-b border-neutral-100 dark:border-neutral-700/60 hover:bg-brand-50 dark:hover:bg-white/5"
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
                    <td
                      className="max-w-48 px-2 py-2 text-xs"
                      title={
                        (p.assigned_staff ?? []).length > 0
                          ? (p.assigned_staff ?? []).join(", ")
                          : "Nobody assigned — HIGH-risk calls email nobody"
                      }
                    >
                      {(p.assigned_staff ?? []).length > 0 ? (
                        <span className="font-semibold text-neutral-700 dark:text-neutral-200">
                          {(p.assigned_staff ?? []).join(", ")}
                        </span>
                      ) : (
                        <span className="font-semibold text-amber-600 dark:text-amber-400">
                          none — no email
                        </span>
                      )}
                    </td>
                    <td className="px-2 py-2 text-xs">
                      {p.active ? (
                        <span className="rounded-full bg-emerald-100 px-2 py-0.5 font-bold text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300">
                          active
                        </span>
                      ) : (
                        <span className="rounded-full bg-slate-100 px-2 py-0.5 font-bold text-slate-500 dark:bg-neutral-800 dark:text-neutral-400">
                          inactive
                        </span>
                      )}
                    </td>
                    <td className="px-2 py-2 text-right">
                      <div className="flex flex-wrap justify-end gap-1.5">
                        {can("manage_patients") && (
                          <button
                            type="button"
                            onClick={() => startEdit(p)}
                            className="cursor-pointer rounded-lg bg-white px-3 py-1.5 text-xs font-bold text-neutral-700 ring-1 ring-neutral-300 transition-all hover:bg-neutral-50 active:scale-95 dark:bg-white/5 dark:text-neutral-200 dark:ring-neutral-600 dark:hover:bg-white/10"
                          >
                            ✎ Edit
                          </button>
                        )}
                        {can("call_patient") && p.active && (
                          <button
                            type="button"
                            onClick={() => setDialTarget(p)}
                            className="cursor-pointer rounded-lg bg-brand-700 px-3 py-1.5 text-xs font-bold text-white shadow-sm transition-all hover:bg-brand-800 hover:shadow active:scale-95"
                          >
                            ☎ Call now
                          </button>
                        )}
                      </div>
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
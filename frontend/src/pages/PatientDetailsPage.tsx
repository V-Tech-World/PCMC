import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import type { Patient, PatientDetail, ScheduleBoard } from "../lib/types";
import CallDetail from "../components/CallDetail";
import { useToast } from "../components/Toast";
import {
  Banner,
  Card,
  EmptyState,
  RiskBadge,
  Spinner,
  fmtDate,
  fmtDateTime,
  fmtDuration,
} from "../components/ui";

/** Step 7 alert states, as small dark-mode-safe chips (same as Calls). */
const ALERT_CHIP: { match: (s: string) => boolean; label: string; style: string }[] = [
  {
    match: (s) => s.startsWith("sent"),
    label: "Sent",
    style: "bg-emerald-100 text-emerald-700 ring-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-800",
  },
  {
    match: (s) => s === "ready",
    label: "Ready",
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
      <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-bold ring-1 ${hit.style}`}>
        {hit.label}
      </span>
    );
  }
  return (
    <span className="text-xs text-neutral-500 dark:text-neutral-400">
      {status.replace(/_/g, " ")}
    </span>
  );
}

/** One label/value pair in the info card. */
function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <dt className="text-[11px] font-bold uppercase tracking-wide text-neutral-400 dark:text-neutral-500">
        {label}
      </dt>
      <dd className="mt-0.5 font-semibold text-neutral-700 dark:text-neutral-200">
        {value === null || value === undefined || value === "" ? (
          <span className="font-normal text-neutral-400 dark:text-neutral-500">—</span>
        ) : (
          value
        )}
      </dd>
    </div>
  );
}

/** "Kamal Perera" -> "KP" (falls back to the patient code). */
function initials(name: string, fallback: string): string {
  const parts = (name || "").trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return fallback.slice(0, 2).toUpperCase();
  return parts
    .slice(0, 2)
    .map((q) => q[0]!.toUpperCase())
    .join("");
}

/**
 * Patient Details (10 Oct 2026): search through every registered patient,
 * pick one, and see the full picture the dashboard/history screens only show
 * in pieces -- the demographics info card (title, gender, age, NIC, contact,
 * discharge), the assigned nurse(s)/doctor(s) by NAME, and the patient's
 * complete call history (rows expand into the same transcript/risk detail
 * and review workflow the Calls screen has).
 */
export default function PatientDetailsPage() {
  const { can } = useAuth();
  const toast = useToast();
  const [params, setParams] = useSearchParams();

  const [patients, setPatients] = useState<Patient[]>([]);
  const [nextCalls, setNextCalls] = useState<Record<string, string | null>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [query, setQuery] = useState("");
  /** Selected patient_code -- kept in ?p= so the view is linkable. */
  const [selected, setSelected] = useState<string | null>(() => params.get("p"));

  const [detail, setDetail] = useState<PatientDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");

  /** Call-history expansion: same open/note/review workflow as CallsPage. */
  const [expanded, setExpanded] = useState<number | null>(null);
  const [noteDraft, setNoteDraft] = useState("");
  const [busy, setBusy] = useState(false);

  const loadList = useCallback(async () => {
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
    void loadList();
  }, [loadList]);

  const loadDetail = useCallback(async (code: string) => {
    setDetailLoading(true);
    setDetailError("");
    setDetail(null);
    setExpanded(null);
    setNoteDraft("");
    try {
      const data = await api.get<PatientDetail>(
        `/records/patients/${encodeURIComponent(code)}`,
      );
      setDetail(data);
    } catch (err) {
      setDetailError(
        err instanceof ApiError ? err.message : "Failed to load the patient.",
      );
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    if (selected) void loadDetail(selected);
    else setDetail(null);
  }, [selected, loadDetail]);

  /** Select a patient and keep ?p=CODE in the URL (shareable link). */
  function pick(code: string) {
    setSelected(code);
    const next = new URLSearchParams(params);
    next.set("p", code);
    setParams(next, { replace: true });
  }

  // External / deep-linked ?p=CODE (e.g. opened in a new tab).
  useEffect(() => {
    const code = params.get("p");
    if (code && code !== selected) setSelected(code);
  }, [params, selected]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return patients;
    return patients.filter((row) =>
      [
        row.patient_code,
        row.name,
        row.title,
        row.phone_number,
        row.nic_number,
        row.diagnosis_category,
        String(row.age ?? ""),
      ].some((v) => (v ?? "").toLowerCase().includes(q)),
    );
  }, [patients, query]);

  async function patch(
    id: number,
    body: Record<string, unknown>,
    successMsg: string,
  ) {
    setBusy(true);
    try {
      await api.patch<{ status: string }>(`/records/calls/${id}`, body);
      toast.ok(successMsg);
      if (selected) await loadDetail(selected);
    } catch (err) {
      toast.error(
        err instanceof ApiError ? err.message : "Could not update the call.",
      );
    } finally {
      setBusy(false);
    }
  }

  const p = detail?.patient ?? null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-extrabold text-ink dark:text-brand-50">
          Patient Details
        </h1>
        <button
          type="button"
          onClick={() => void loadList()}
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-lg bg-white px-3 py-1.5 text-xs font-bold text-neutral-700 ring-1 ring-neutral-300 transition-all hover:bg-neutral-50 active:scale-95 dark:bg-white/5 dark:text-neutral-200 dark:ring-neutral-600 dark:hover:bg-white/10"
        >
          {loading && <Spinner className="h-3 w-3" />}
          Refresh
        </button>
      </div>

      {error && <Banner kind="error">{error}</Banner>}

      <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
        {/* ---------------- search + patient list ---------------- */}
        <Card title={`${filtered.length} of ${patients.length} patient(s)`}>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search code, name, phone, NIC…"
            className="w-full rounded-lg border border-neutral-300 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200 dark:border-neutral-600 dark:bg-[#131c33] dark:text-neutral-100"
          />
          {loading ? (
            <span className="mt-3 inline-flex items-center gap-2 text-sm text-neutral-500 dark:text-neutral-400">
              <Spinner /> Loading…
            </span>
          ) : filtered.length === 0 ? (
            <p className="mt-3 text-sm text-neutral-500 dark:text-neutral-400">
              No patient matches “{query}”.
            </p>
          ) : (
            <ul className="mt-3 max-h-[60vh] space-y-1 overflow-y-auto pr-1">
              {filtered.map((row) => (
                <li key={row.patient_code}>
                  <button
                    type="button"
                    onClick={() => pick(row.patient_code)}
                    className={`w-full cursor-pointer rounded-lg px-3 py-2 text-left transition-all ${
                      selected === row.patient_code
                        ? "bg-brand-700 text-white shadow"
                        : "hover:bg-brand-50 dark:hover:bg-white/5"
                    }`}
                  >
                    <span className="block text-sm font-bold">
                      {row.title ? `${row.title} ` : ""}
                      {row.name || row.patient_code}
                    </span>
                    <span
                      className={`block text-xs ${
                        selected === row.patient_code
                          ? "text-white/80"
                          : "text-neutral-500 dark:text-neutral-400"
                      }`}
                    >
                      {row.patient_code} · {row.phone_number}
                      {!row.active && " · inactive"}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        {/* ---------------- selected patient ---------------- */}
        <div className="space-y-4">
          {!selected ? (
            <Card>
              <EmptyState
                title="Select a patient"
                hint="Search on the left and pick a patient to see their info, care team and call history."
                icon="🧑‍⚕️"
              />
            </Card>
          ) : detailLoading ? (
            <Card>
              <span className="inline-flex items-center gap-2 text-sm text-neutral-500 dark:text-neutral-400">
                <Spinner /> Loading {selected}…
              </span>
            </Card>
          ) : detailError ? (
            <Banner kind="error">{detailError}</Banner>
          ) : detail && p ? (
            <>
              {/* ------- info card ------- */}
              <Card
                title="Patient information"
                actions={
                  <span
                    className={`rounded-full px-2 py-0.5 text-[11px] font-bold uppercase ${
                      p.active
                        ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300"
                        : "bg-slate-100 text-slate-500 dark:bg-neutral-800 dark:text-neutral-400"
                    }`}
                  >
                    {p.active ? "active" : "inactive"}
                  </span>
                }
              >
                <div className="flex items-center gap-4">
                  <span className="grid h-14 w-14 shrink-0 place-items-center rounded-full bg-brand-100 text-lg font-extrabold text-brand-800 dark:bg-[#122347] dark:text-brand-200">
                    {initials(p.name, p.patient_code)}
                  </span>
                  <div className="min-w-0">
                    <div className="truncate text-lg font-extrabold text-neutral-800 dark:text-neutral-100">
                      {p.title ? `${p.title} ` : ""}
                      {p.name || "(no name)"}
                    </div>
                    <div className="text-sm text-neutral-500 dark:text-neutral-400">
                      {p.patient_code} · {p.diagnosis_category} discharge
                      {p.discharge_date ? ` · ${fmtDate(p.discharge_date)}` : ""}
                    </div>
                  </div>
                </div>

                <dl className="mt-4 grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
                  <Field label="Phone" value={p.phone_number} />
                  <Field label="Gender" value={p.gender} />
                  <Field label="Age" value={p.age != null ? `${p.age} years` : null} />
                  <Field label="NIC number" value={p.nic_number} />
                  <Field label="Preferred language" value={p.language_pref} />
                  <Field label="Next check-in" value={nextCalls[p.patient_code]} />
                  <Field label="Discharged on" value={fmtDate(p.discharge_date)} />
                  <Field label="Registered" value={fmtDate(p.created_at ?? null)} />
                  <Field label="Notes" value={p.notes} />
                </dl>
              </Card>

              {/* ------- care team ------- */}
              <Card title={`Assigned care team — ${detail.care_team.length}`}>
                {detail.care_team.length === 0 ? (
                  <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm font-semibold text-amber-700 dark:border-amber-900 dark:bg-amber-950/50 dark:text-amber-300">
                    Nobody assigned — a HIGH-risk call for this patient emails
                    nobody. An admin can assign nurse(s)/doctor(s) from the
                    Patients screen (Edit).
                  </p>
                ) : (
                  <ul className="grid gap-2 sm:grid-cols-2">
                    {detail.care_team.map((m) => (
                      <li
                        key={m.username}
                        className="flex items-center gap-3 rounded-lg border border-neutral-200 px-3 py-2 dark:border-neutral-700"
                      >
                        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-sky-100 text-xs font-extrabold text-sky-800 dark:bg-sky-950 dark:text-sky-300">
                          {initials(m.display_name, m.username)}
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-sm font-bold text-neutral-800 dark:text-neutral-100">
                            {m.display_name}
                            {!m.active && (
                              <span className="ml-1.5 rounded-full bg-red-100 px-1.5 py-0.5 text-[10px] font-bold uppercase text-red-700 dark:bg-red-950 dark:text-red-300">
                                deactivated
                              </span>
                            )}
                          </div>
                          <div className="truncate text-xs text-neutral-500 dark:text-neutral-400">
                            {m.email || "no email on file"}
                          </div>
                        </div>
                        <span
                          className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-bold uppercase ${
                            m.role === "doctor"
                              ? "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300"
                              : m.role === "nurse"
                                ? "bg-brand-100 text-brand-800 dark:bg-[#122347] dark:text-brand-200"
                                : "bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300"
                          }`}
                        >
                          {m.role}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </Card>

              {/* ------- call history ------- */}
              <Card title={`Call history — ${detail.call_count} call(s)`}>
                {detail.calls.length === 0 ? (
                  <EmptyState
                    title="No calls yet"
                    hint="Place a call from the Patients screen, or wait for the scheduler."
                    icon="☎️"
                  />
                ) : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-left text-sm">
                      <thead>
                        <tr className="border-b border-neutral-200 dark:border-neutral-700 text-xs uppercase tracking-wide text-neutral-400 dark:text-neutral-500">
                          <th className="px-2 py-2">When</th>
                          <th className="px-2 py-2">Category</th>
                          <th className="px-2 py-2">Risk</th>
                          <th className="px-2 py-2">Duration</th>
                          <th className="px-2 py-2">Alert</th>
                          <th className="px-2 py-2">State</th>
                        </tr>
                      </thead>
                      <tbody>
                        {detail.calls.map((row) => (
                          <Fragment key={row.id}>
                            <tr
                              onClick={() =>
                                setExpanded(expanded === row.id ? null : row.id)
                              }
                              className={`cursor-pointer border-b border-neutral-100 dark:border-neutral-700/60 hover:bg-brand-50 dark:hover:bg-white/5 ${
                                expanded === row.id
                                  ? "bg-brand-50 dark:bg-brand-900/30"
                                  : ""
                              }`}
                            >
                              <td className="whitespace-nowrap px-2 py-2">
                                <span
                                  className={`mr-1.5 inline-block text-neutral-400 transition-transform duration-200 dark:text-neutral-500 ${
                                    expanded === row.id ? "rotate-90" : ""
                                  }`}
                                  aria-hidden="true"
                                >
                                  ▸
                                </span>
                                {fmtDateTime(row.started_at)}
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
                              <td className="px-2 py-2">
                                <AlertChip status={row.alert_status} />
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
                              <tr className="border-b border-brand-100 bg-brand-50/40 dark:border-brand-900 dark:bg-brand-900/20">
                                <td colSpan={6} className="page-enter px-4 py-4">
                                  <CallDetail
                                    row={row}
                                    noteDraft={noteDraft}
                                    setNoteDraft={setNoteDraft}
                                    busy={busy}
                                    canReview={can("review_call")}
                                    canClose={can("close_case")}
                                    canAlert={can("review_call")}
                                    onSaveNote={() =>
                                      void patch(
                                        row.id,
                                        { nurse_note: noteDraft },
                                        "Case note saved.",
                                      )
                                    }
                                    onMarkReviewed={() =>
                                      void patch(
                                        row.id,
                                        { reviewed: true, nurse_note: noteDraft },
                                        "Marked as reviewed ✓",
                                      )
                                    }
                                    onClose={() =>
                                      void patch(
                                        row.id,
                                        { close_case: true },
                                        "Case closed ✓",
                                      )
                                    }
                                    onAlertSent={(msg, isError) => {
                                      if (isError) toast.error(msg);
                                      else toast.ok(msg);
                                      if (selected) void loadDetail(selected);
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
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}



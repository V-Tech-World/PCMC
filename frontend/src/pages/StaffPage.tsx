import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "../lib/api";
import type { Role, StaffUser } from "../lib/types";
import { useToast } from "../components/Toast";
import { Banner, Card, EmptyState, Spinner, fmtDateTime } from "../components/ui";

const ROLES: Role[] = ["nurse", "doctor", "admin"];

interface StaffList {
  count: number;
  staff: StaffUser[];
}

/**
 * Staff accounts (admin only -- the route is already guarded by
 * manage_staff). This is how the demo creates the nurse/doctor logins that
 * Step 9 TC1 exercises alongside the seeded admin.
 */
export default function StaffPage() {
  const toast = useToast();
  const [staff, setStaff] = useState<StaffUser[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState({
    username: "",
    password: "",
    role: "nurse",
    display_name: "",
  });
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await api.get<StaffList>("/auth/staff");
      setStaff(data.staff);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load staff.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function createStaff(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.post<{ status: string }>("/auth/staff", form);
      toast.ok(`Account "${form.username}" created (${form.role}).`);
      setForm({ username: "", password: "", role: "nurse", display_name: "" });
      await load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not create.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-extrabold text-ink dark:text-brand-50">Staff accounts</h1>

      {error && <Banner kind="error">{error}</Banner>}

      <Card title="Create a login">
        <form onSubmit={createStaff} className="grid gap-3 sm:grid-cols-2">
          <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
            Employee ID *
            <input
              required
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
              placeholder="nur002"
              className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#131c33] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
            />
          </label>
          <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
            Password * (min 6 chars)
            <input
              required
              minLength={6}
              type="password"
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#131c33] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
            />
          </label>
          <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
            Display name
            <input
              value={form.display_name}
              onChange={(e) => setForm({ ...form, display_name: e.target.value })}
              placeholder="Nurse Nimali"
              className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#131c33] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
            />
          </label>
          <label className="text-sm font-semibold text-neutral-600 dark:text-neutral-300">
            Role
            <select
              value={form.role}
              onChange={(e) => setForm({ ...form, role: e.target.value })}
              className="mt-1 w-full rounded-lg border border-neutral-300 dark:border-neutral-600 dark:bg-[#131c33] dark:text-neutral-100 px-3 py-2 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </label>
          <div className="sm:col-span-2">
            <button
              type="submit"
              disabled={busy}
              className="inline-flex cursor-pointer items-center gap-2 rounded-lg bg-brand-700 px-4 py-2 text-sm font-bold text-white shadow-sm transition-all hover:bg-brand-800 hover:shadow active:scale-95 disabled:opacity-50"
            >
              {busy && <Spinner className="h-4 w-4" />}
              {busy ? "Creating…" : "Create account"}
            </button>
          </div>
        </form>
      </Card>

      <Card title={`${staff.length} account(s)`}>
        {loading ? (
          <span className="inline-flex items-center gap-2 text-sm text-neutral-500 dark:text-neutral-400">
            <Spinner /> Loading…
          </span>
        ) : staff.length === 0 ? (
          <EmptyState
            title="No staff accounts yet"
            hint="Create the first login with the form above."
            icon="👥"
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-neutral-200 dark:border-neutral-700 text-xs uppercase tracking-wide text-neutral-400 dark:text-neutral-500">
                  <th className="px-2 py-2">Employee ID</th>
                  <th className="px-2 py-2">Name</th>
                  <th className="px-2 py-2">Role</th>
                  <th className="px-2 py-2">Hospital</th>
                  <th className="px-2 py-2">Active</th>
                  <th className="px-2 py-2">Last login</th>
                </tr>
              </thead>
              <tbody>
                {staff.map((u) => (
                  <tr
                    key={u.username}
                    className="border-b border-neutral-100 dark:border-neutral-700/60 hover:bg-brand-50 dark:hover:bg-white/5"
                  >
                    <td className="px-2 py-2 font-semibold">{u.username}</td>
                    <td className="px-2 py-2">{u.display_name}</td>
                    <td className="px-2 py-2">
                      <span className="rounded-full bg-brand-100 px-2 py-0.5 text-xs font-bold uppercase text-brand-800 dark:bg-[#122347] dark:text-brand-200">
                        {u.role}
                      </span>
                    </td>
                    <td className="px-2 py-2 text-xs">{u.hospital || "--"}</td>
                    <td className="px-2 py-2 text-xs">
                      {u.active ? "yes" : "no"}
                    </td>
                    <td className="whitespace-nowrap px-2 py-2 text-xs">
                      {fmtDateTime(u.last_login_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
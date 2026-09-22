import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Spinner } from "../components/ui";

/**
 * Login screen (Step 9, matches designs/73ef74e7-...png): green gradient
 * backdrop, white rounded card, VoiceCare LK logo, role tabs, hospital,
 * Employee ID + password, green "Login to Dashboard" button.
 *
 * The role tabs only pick the Employee-ID placeholder -- the real role comes
 * from the account (nurse|doctor|admin), so tabs can never escalate rights.
 */
const ROLE_TABS = [
  { id: "doctor", label: "Doctor", placeholder: "DR001" },
  { id: "nurse", label: "Nurse", placeholder: "NUR001" },
  { id: "admin", label: "Administrator", placeholder: "ADM001" },
] as const;

export default function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [tab, setTab] = useState<(typeof ROLE_TABS)[number]["id"]>("doctor");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [hospital, setHospital] = useState("Colombo National Hospital");
  const [error, setError] = useState("");
  const [hint, setHint] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [busy, setBusy] = useState(false);

  const activeTab = ROLE_TABS.find((t) => t.id === tab) ?? ROLE_TABS[0];

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      await login(username.trim(), password);
      navigate("/", { replace: true });
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Login failed -- try again.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative grid min-h-screen place-items-center overflow-hidden bg-gradient-to-br from-brand-500 to-brand-800 p-4">
      {/* decorative floating orbs (purely visual) */}
      <div
        className="float-slow pointer-events-none absolute -left-24 top-10 h-72 w-72 rounded-full bg-white/10 blur-2xl"
        aria-hidden="true"
      />
      <div
        className="float-slow pointer-events-none absolute -bottom-16 -right-10 h-80 w-80 rounded-full bg-brand-900/25 blur-2xl"
        style={{ animationDelay: "1.5s" }}
        aria-hidden="true"
      />
      <div className="pop-in relative w-full max-w-md rounded-2xl bg-white p-8 shadow-2xl">
        <div className="text-center">
          <img
            src="/logo-horizontal.svg"
            alt="VoiceCare LK"
            className="mx-auto h-14 w-auto"
          />
          <p className="mt-3 text-sm text-neutral-500">
            Post-Discharge Patient Care and Monitoring System
          </p>
          <p className="text-sm text-neutral-500">Colombo National Hospital</p>
        </div>

        <div
          className="mt-6 grid grid-cols-3 rounded-lg bg-neutral-100 p-1"
          role="tablist"
          aria-label="Role"
        >
          {ROLE_TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              onClick={() => setTab(t.id)}
              className={`rounded-md py-2 text-sm font-semibold transition-colors ${
                tab === t.id
                  ? "bg-white text-brand-700 shadow"
                  : "text-neutral-500 hover:text-neutral-700"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        <form onSubmit={onSubmit} className="mt-6 space-y-4">
          <div>
            <label
              htmlFor="hospital"
              className="mb-1 block text-sm font-bold text-neutral-700"
            >
              Hospital
            </label>
            <select
              id="hospital"
              value={hospital}
              onChange={(e) => setHospital(e.target.value)}
              className="w-full rounded-lg border border-neutral-300 px-3 py-2.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
            >
              <option>Colombo National Hospital</option>
            </select>
          </div>

          <div>
            <label
              htmlFor="employee-id"
              className="mb-1 block text-sm font-bold text-neutral-700"
            >
              Employee ID
            </label>
            <input
              id="employee-id"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder={activeTab.placeholder}
              autoComplete="username"
              required
              className="w-full rounded-lg border border-neutral-300 px-3 py-2.5 text-sm focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
            />
          </div>

          <div>
            <label
              htmlFor="password"
              className="mb-1 block text-sm font-bold text-neutral-700"
            >
              Password
            </label>
            <div className="relative">
              <input
                id="password"
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                autoComplete="current-password"
                required
                className="w-full rounded-lg border border-neutral-300 px-3 py-2.5 pr-11 text-sm transition-shadow focus:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200"
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? "Hide password" : "Show password"}
                className="absolute right-2 top-1/2 -translate-y-1/2 cursor-pointer rounded-md p-1.5 text-neutral-400 transition-colors hover:text-brand-700"
              >
                {showPassword ? (
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="h-[18px] w-[18px]"
                    aria-hidden="true"
                  >
                    <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94" />
                    <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19" />
                    <path d="M14.12 14.12a3 3 0 1 1-4.24-4.24" />
                    <path d="M3 3l18 18" />
                  </svg>
                ) : (
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.8"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="h-[18px] w-[18px]"
                    aria-hidden="true"
                  >
                    <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 19 1 12z" />
                    <circle cx="12" cy="12" r="3" />
                  </svg>
                )}
              </button>
            </div>
          </div>

          {error && (
            <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm font-medium text-red-700">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy}
            className="inline w-full cursor-pointer rounded-lg bg-brand-700 py-3 text-sm font-bold uppercase tracking-wide text-white shadow-lg shadow-brand-700/25 transition-all hover:-translate-y-0.5 hover:bg-brand-800 hover:shadow-xl active:translate-y-0 disabled:opacity-60"
          >
            <span className="inline-flex items-center justify-center gap-2">
              {busy && <Spinner className="h-4 w-4" />}
              {busy ? "Signing in…" : "Login to Dashboard"}
            </span>
          </button>
        </form>

        <div className="mt-4 text-center">
          <button
            type="button"
            onClick={() =>
              setHint("Demo build -- ask your system administrator to reset it.")
            }
            className="text-sm font-semibold text-brand-700 hover:underline"
          >
            Forgot password?
          </button>
          {hint && <p className="mt-1 text-xs text-neutral-500">{hint}</p>}
        </div>
      </div>
    </div>
  );
}
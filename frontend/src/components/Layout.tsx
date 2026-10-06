import { useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../lib/auth";
import ScrollDownIndicator from "./ScrollDownIndicator";
import ThemeToggle from "./ThemeToggle";

/** Tiny inline-SVG icon set (no icon dependency). */
function Ico({
  children,
  className = "h-5 w-5",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

const ICON_GRID = (
  <>
    <rect x="3" y="3" width="7" height="9" rx="1" />
    <rect x="14" y="3" width="7" height="5" rx="1" />
    <rect x="14" y="12" width="7" height="9" rx="1" />
    <rect x="3" y="16" width="7" height="5" rx="1" />
  </>
);
const ICON_PHONE = (
  <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z" />
);
const ICON_USERS = (
  <>
    <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <path d="M22 21v-2a4 4 0 0 0-3-3.87" />
    <path d="M16 3.13a4 4 0 0 1 0 7.75" />
  </>
);
const ICON_CALENDAR = (
  <>
    <rect x="3" y="4" width="18" height="18" rx="2" />
    <path d="M16 2v4M8 2v4M3 10h18" />
  </>
);
const ICON_SHIELD = (
  <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
);
const ICON_LOGOUT = (
  <>
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
    <path d="M16 17l5-5-5-5M21 12H9" />
  </>
);
const ICON_MENU = <path d="M4 6h16M4 12h16M4 18h16" />;
const ICON_USER = (
  <>
    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
    <circle cx="12" cy="7" r="4" />
  </>
);
/** Patient Details: an ID card (info sheet per patient). */
const ICON_ID_CARD = (
  <>
    <rect x="3" y="5" width="18" height="14" rx="2" />
    <circle cx="8.5" cy="11" r="2" />
    <path d="M13 10h5M13 14h5M5.5 16.5c.7-1.4 1.9-2.1 3-2.1s2.3.7 3 2.1" />
  </>
);

interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  end?: boolean;
  perm?: string;
}

const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Dashboard", icon: ICON_GRID, end: true },
  { to: "/calls", label: "Calls", icon: ICON_PHONE },
  { to: "/patients", label: "Patients", icon: ICON_USERS },
  { to: "/patient-details", label: "Patient Details", icon: ICON_ID_CARD },
  { to: "/schedule", label: "Schedule", icon: ICON_CALENDAR },
  { to: "/staff", label: "Staff", icon: ICON_SHIELD, perm: "manage_staff" },
];

/**
 * App shell from the dashboard sketch: dark full-height sidebar (nav +
 * Logout pinned at the bottom), light top bar with the profile icon on the
 * right, white content area. Collapses to a drawer under lg (TC4).
 */
export default function Layout() {
  const { user, logout, can } = useAuth();
  const location = useLocation();
  const [drawerOpen, setDrawerOpen] = useState(false);

  const items = NAV_ITEMS.filter((item) => !item.perm || can(item.perm));

  return (
    <div className="flex min-h-screen bg-neutral-50 dark:bg-[#0b1220]">
      {drawerOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/40 lg:hidden"
          onClick={() => setDrawerOpen(false)}
          aria-hidden="true"
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-60 flex-col bg-neutral-700 dark:bg-[#111a2e] transition-transform duration-200 lg:static lg:translate-x-0 ${
          drawerOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex h-16 shrink-0 items-center justify-center border-b border-white/10 px-3">
          <img
            src="/logo-horizontal-light.svg"
            alt="VoiceCare"
            className="h-9 w-auto"
          />
        </div>

        <nav className="flex-1 overflow-y-auto" aria-label="Main">
          {items.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              onClick={() => setDrawerOpen(false)}
              className={({ isActive }) =>
                `flex cursor-pointer items-center gap-3 border-b border-white/5 px-5 py-3.5 text-sm font-medium transition-all hover:pl-6 ${
                  isActive
                    ? "bg-brand-600 text-white"
                    : "text-neutral-200 hover:bg-white/10 dark:text-neutral-300 dark:hover:bg-white/5"
                }`
              }
            >
              <Ico>{item.icon}</Ico>
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>

        <button
          type="button"
          onClick={logout}
          className="group flex shrink-0 cursor-pointer items-center justify-center gap-2 border-t border-white/10 bg-gradient-to-r from-red-50 to-red-100 py-4 text-sm font-extrabold uppercase tracking-wider text-red-700 transition-all hover:from-red-600 hover:to-red-700 hover:text-white hover:shadow-inner active:scale-[0.99] dark:from-red-950/70 dark:to-red-950/40 dark:text-red-300 dark:hover:from-red-600 dark:hover:to-red-700 dark:hover:text-white"
        >
          <Ico className="h-4 w-4 transition-transform duration-200 group-hover:rotate-90">
            {ICON_LOGOUT}
          </Ico>
          Logout
        </button>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-16 items-center justify-between gap-3 bg-neutral-200 px-4 shadow-sm dark:border-b dark:border-white/5 dark:bg-[#16203a]">
          <div className="flex min-w-0 items-center gap-3">
            <button
              type="button"
              className="cursor-pointer rounded-lg p-2 text-neutral-700 transition-all hover:bg-white/60 active:scale-90 dark:text-neutral-200 dark:hover:bg-white/10 lg:hidden"
              onClick={() => setDrawerOpen(true)}
              aria-label="Open menu"
            >
              <Ico>{ICON_MENU}</Ico>
            </button>
            <span className="truncate text-sm font-bold text-neutral-700 dark:text-neutral-200">
              {user?.hospital || "VoiceCare"}
            </span>
          </div>

          <div className="flex items-center gap-3">
            <ThemeToggle />
            <div className="hidden text-right leading-tight sm:block">
              <div className="text-sm font-bold text-neutral-800 dark:text-neutral-100">
                {user?.display_name}
              </div>
              <div className="text-xs font-semibold uppercase tracking-wide text-brand-700 dark:text-brand-300">
                {user?.role}
              </div>
            </div>
            <span
              className="grid h-10 w-10 shrink-0 place-items-center rounded-full border-2 border-neutral-500 bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-200"
              title={user?.username}
            >
              <Ico className="h-6 w-6">{ICON_USER}</Ico>
            </span>
          </div>
        </header>

        <main className="flex-1 p-4 sm:p-6">
          <div key={location.pathname} className="page-enter mx-auto max-w-7xl">
            <Outlet />
          </div>
        </main>
        <ScrollDownIndicator />
      </div>
    </div>
  );
}
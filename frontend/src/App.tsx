import type { ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import { ToastProvider } from "./components/Toast";
import { AuthProvider, useAuth } from "./lib/auth";
import CallsPage from "./pages/CallsPage";
import DashboardPage from "./pages/DashboardPage";
import LoginPage from "./pages/LoginPage";
import PatientsPage from "./pages/PatientsPage";
import SchedulePage from "./pages/SchedulePage";
import StaffPage from "./pages/StaffPage";

function Splash() {
  return (
    <div className="grid min-h-screen place-items-center bg-gradient-to-br from-brand-500 to-brand-700">
      <div className="text-center text-white">
        <img src="/icon-master.svg" alt="" className="mx-auto h-16 w-16" />
        <p className="mt-4 font-semibold">Loading VoiceCare LK…</p>
      </div>
    </div>
  );
}

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, initializing } = useAuth();
  if (initializing) return <Splash />;
  if (!user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

/** Route-level role guard (hides whole screens, not just buttons). */
function RequirePerm({
  permission,
  children,
}: {
  permission: string;
  children: ReactNode;
}) {
  const { user, initializing, can } = useAuth();
  if (initializing) return <Splash />;
  if (!user) return <Navigate to="/login" replace />;
  if (!can(permission)) return <Navigate to="/" replace />;
  return <>{children}</>;
}

function LoginGate() {
  const { user, initializing } = useAuth();
  if (initializing) return <Splash />;
  if (user) return <Navigate to="/" replace />;
  return <LoginPage />;
}

export default function App() {
  return (
    <ToastProvider>
      <AuthProvider>
        <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginGate />} />
          <Route
            element={
              <RequireAuth>
                <Layout />
              </RequireAuth>
            }
          >
            <Route index element={<DashboardPage />} />
            <Route path="calls" element={<CallsPage />} />
            <Route path="patients" element={<PatientsPage />} />
            <Route path="schedule" element={<SchedulePage />} />
            <Route
              path="staff"
              element={
                <RequirePerm permission="manage_staff">
                  <StaffPage />
                </RequirePerm>
              }
            />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
      </AuthProvider>
    </ToastProvider>
  );
}
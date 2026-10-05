import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  api,
  apiFetch,
  clearSession,
  loadPerms,
  loadToken,
  loadUser,
  saveSession,
} from "./api";
import type {
  LoginResponse,
  MeResponse,
  RolePermissions,
  RolesPayload,
  StaffUser,
} from "./types";

/**
 * Session state for the whole dashboard (Step 9 TC1): who is logged in,
 * which role permissions they have (GET /auth/roles), and `can(perm)` so
 * each screen can show only the actions that role is allowed to perform.
 */
interface AuthContextValue {
  user: StaffUser | null;
  perms: RolePermissions | null;
  initializing: boolean;
  login: (username: string, password: string) => Promise<StaffUser>;
  logout: () => void;
  can: (permission: string) => boolean;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<StaffUser | null>(() => loadUser());
  const [perms, setPerms] = useState<RolePermissions | null>(() => loadPerms());
  const [initializing, setInitializing] = useState<boolean>(() => !!loadToken());

  // Restore a stored session on reload: confirm the token, refresh identity
  // and the permission matrix in one pass.
  useEffect(() => {
    const token = loadToken();
    if (!token) {
      setInitializing(false);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const [me, roles] = await Promise.all([
          api.get<MeResponse>("/auth/me"),
          api.get<RolesPayload>("/auth/roles"),
        ]);
        if (cancelled) return;
        if (me.user) {
          setUser(me.user);
          setPerms(roles.permissions);
          saveSession(token, me.user, roles.permissions);
        } else {
          clearSession();
          setUser(null);
          setPerms(null);
        }
      } catch {
        if (!cancelled) {
          clearSession();
          setUser(null);
          setPerms(null);
        }
      } finally {
        if (!cancelled) setInitializing(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const [resp, roles] = await Promise.all([
      apiFetch<LoginResponse>("/auth/login", {
        method: "POST",
        body: { username, password },
        suppress401Handler: true,
      }),
      api.get<RolesPayload>("/auth/roles"),
    ]);
    saveSession(resp.access_token, resp.user, roles.permissions);
    setUser(resp.user);
    setPerms(roles.permissions);
    return resp.user;
  }, []);

  const logout = useCallback(() => {
    clearSession();
    setUser(null);
    setPerms(null);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      perms,
      initializing,
      login,
      logout,
      can: (permission: string) => {
        if (!user) return false;
        const allowed = perms?.[permission];
        return Array.isArray(allowed) && allowed.includes(user.role);
      },
    }),
    [user, perms, initializing, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
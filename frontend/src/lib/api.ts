import type { RolesPayload, StaffUser } from "./types";

/**
 * Thin fetch wrapper for the VoiceCare backend (Step 9).
 *
 * - Attaches `Authorization: Bearer <jwt>` from localStorage on every call.
 * - On 401 it clears the session and bounces to /login (unless the caller is
 *   the login form itself), so an expired token can never show stale screens.
 * - The browser never carries the machine X-Api-Key: staff tokens are enough.
 */
const API_BASE = (
  (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000"
).replace(/\/$/, "");

const TOKEN_KEY = "voicecare_token";
const USER_KEY = "voicecare_user";
const PERMS_KEY = "voicecare_perms";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function saveSession(
  token: string,
  user: StaffUser,
  perms: RolesPayload["permissions"],
): void {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
  localStorage.setItem(PERMS_KEY, JSON.stringify(perms));
}

export function loadToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function loadUser(): StaffUser | null {
  const raw = localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as StaffUser;
  } catch {
    return null;
  }
}

export function loadPerms(): RolesPayload["permissions"] | null {
  const raw = localStorage.getItem(PERMS_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as RolesPayload["permissions"];
  } catch {
    return null;
  }
}

export function clearSession(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  localStorage.removeItem(PERMS_KEY);
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** Login attempts must see the server's 401 detail, not the redirect. */
  suppress401Handler?: boolean;
}

export async function apiFetch<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const method = options.method ?? (options.body !== undefined ? "POST" : "GET");
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = loadToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";

  let res: Response;
  try {
    res = await fetch(API_BASE + path, {
      method,
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    });
  } catch {
    throw new ApiError(
      0,
      `Cannot reach the backend at ${API_BASE} -- is it running?`,
    );
  }

  if (res.status === 401 && !options.suppress401Handler) {
    clearSession();
    if (!window.location.pathname.startsWith("/login")) {
      window.location.assign("/login");
    }
    throw new ApiError(401, "Session expired -- please log in again.");
  }

  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    const detail =
      typeof data === "object" && data !== null && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : `Request failed (HTTP ${res.status}).`;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string): Promise<T> => apiFetch<T>(path),
  post: <T>(path: string, body?: unknown): Promise<T> =>
    apiFetch<T>(path, { method: "POST", body: body ?? {} }),
  patch: <T>(path: string, body: unknown): Promise<T> =>
    apiFetch<T>(path, { method: "PATCH", body }),
  del: <T>(path: string): Promise<T> => apiFetch<T>(path, { method: "DELETE" }),
};

export { API_BASE };
import axios from "axios";
import type { AxiosError } from "axios";

import { resolveApiBase } from "../lib/apiBase";
import { useSessionStore } from "../stores/sessionStore";

const baseURL = resolveApiBase();

function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`;
  for (const part of document.cookie.split(";")) {
    const value = part.trim();
    if (value.startsWith(prefix)) return decodeURIComponent(value.slice(prefix.length));
  }
  return null;
}

export const apiClient = axios.create({
  baseURL,
  timeout: 30_000, // 30 second timeout for all requests
});

// ── Refresh-token helpers ──────────────────────────────────────────────────

let _refreshPromise: Promise<string | null> | null = null;

async function _tryRefreshToken(): Promise<string | null> {
  const store = useSessionStore.getState();
  if (!store.token) return null;

  try {
    const { data } = await axios.post<{ access_token: string; expires_at: string }>(
      `${baseURL}/auth/refresh`,
      undefined,
      {
        withCredentials: true,
        headers: { "X-CSRF-Token": readCookie("hermeshq_csrf") ?? "" },
      },
    );
    const newToken = data.access_token;
    store.setToken(newToken);
    return newToken;
  } catch {
    return null;
  }
}

function _queuedRefresh(): Promise<string | null> {
  if (!_refreshPromise) {
    _refreshPromise = _tryRefreshToken().finally(() => {
      _refreshPromise = null;
    });
  }
  return _refreshPromise;
}

// ── Interceptors ───────────────────────────────────────────────────────────

apiClient.interceptors.request.use((config) => {
  const token = useSessionStore.getState().token;
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  const csrfToken = readCookie("hermeshq_csrf");
  if (csrfToken) config.headers.set("X-CSRF-Token", csrfToken);
  // Include cookies (for httpOnly auth cookie support)
  config.withCredentials = true;
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const status = error.response?.status;
    const requestUrl = error.config?.url ?? "";
    const isLoginAttempt = requestUrl.includes("/auth/login") || requestUrl.includes("/auth/refresh");
    const hadAuthHeader = Boolean(error.config?.headers?.Authorization);

    // If 401 and we have a session, try a token refresh once before logging out
    const configAny = error.config as (typeof error.config & { _retry?: boolean }) | undefined;
    const alreadyRetried = configAny?._retry === true;
    if (status === 401 && hadAuthHeader && !isLoginAttempt && !alreadyRetried) {
      const hasSession = Boolean(useSessionStore.getState().token);
      if (hasSession) {
        const newToken = await _queuedRefresh();
        if (newToken && error.config) {
          // Clone the original request with the new token and retry once
          const retryConfig = error.config as typeof error.config & { _retry?: boolean };
          retryConfig._retry = true;
          retryConfig.headers.set("Authorization", `Bearer ${newToken}`);
          return apiClient.request(retryConfig);
        }
        // Refresh also failed → log out
        useSessionStore.getState().logout();
      }
    }

    return Promise.reject(error);
  },
);

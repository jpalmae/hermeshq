import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";
import { resolveApiBase } from "../lib/apiBase";
import { queryClient } from "../lib/queryClient";
import { useSessionStore } from "../stores/sessionStore";
import type { AuthProvidersResponse, User } from "../types/api";

export interface MfaRequiredResponse {
  mfa_required: true;
  mfa_token: string;
  email_mask: string | null;
  expires_at: string;
}

export interface LoginResponse {
  access_token?: string;
  expires_at?: string;
  mfa_required?: boolean;
  mfa_token?: string;
  email_mask?: string | null;
}

export async function login(username: string, password: string) {
  const { data } = await apiClient.post<LoginResponse>(
    "/auth/login",
    { username, password },
  );
  return data;
}

export async function verifyMfa(mfaToken: string, code: string) {
  const { data } = await apiClient.post<{ access_token: string; expires_at: string }>(
    "/auth/mfa/verify",
    { mfa_token: mfaToken, code },
  );
  return data;
}

export async function resendMfa(mfaToken: string) {
  const { data } = await apiClient.post<MfaRequiredResponse>(
    "/auth/mfa/resend",
    { mfa_token: mfaToken },
  );
  return data;
}

export function useAuthProviders() {
  return useQuery({
    queryKey: ["auth-providers"],
    queryFn: async () => {
      const { data } = await apiClient.get<AuthProvidersResponse>("/auth/providers");
      return data;
    },
    retry: false,
  });
}

export function buildOidcLoginUrl(provider?: string) {
  const base = resolveApiBase();
  if (provider) {
    return `${base}/auth/oidc/login?provider=${encodeURIComponent(provider)}`;
  }
  return `${base}/auth/oidc/login`;
}

export function buildOidcLogoutUrl() {
  const base = resolveApiBase();
  return `${base}/auth/oidc/logout`;
}

export function useMe(enabled: boolean) {
  return useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      const { data } = await apiClient.get<User>("/auth/me");
      return data;
    },
    enabled,
    retry: false,
  });
}

export function useUpdateMyPreferences() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: {
      theme_preference?: "default" | "dark" | "light" | "system" | "enterprise" | "sixmanager" | "sixmanager-light";
      locale_preference?: "default" | "en" | "es";
    }) => {
      const { data } = await apiClient.put<User>("/auth/me/preferences", payload);
      return data;
    },
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      return data;
    },
  });
}

export function useUpdateMyProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: { display_name: string }) => {
      const { data } = await apiClient.put<User>("/auth/me/profile", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

export function useChangeMyPassword() {
  return useMutation({
    mutationFn: async (payload: { current_password: string; new_password: string }) => {
      await apiClient.put("/auth/me/password", payload);
    },
  });
}

export function useUploadMyAvatar() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (file: File) => {
      const formData = new FormData();
      formData.append("file", file);
      const { data } = await apiClient.post<User>("/auth/me/avatar", formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

export function useDeleteMyAvatar() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data } = await apiClient.delete<User>("/auth/me/avatar");
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });
}

export async function refreshToken() {
  const { data } = await apiClient.post<{ access_token: string; expires_at: string }>(
    "/auth/refresh",
  );
  return data;
}

// Exchange the httpOnly OIDC cookie for an access token after an OIDC login
// redirect (the JWT is never exposed in the URL).
export async function exchangeOidcCookie(): Promise<string | null> {
  try {
    const { access_token } = await refreshToken();
    return access_token ?? null;
  } catch {
    return null;
  }
}

export async function forgotPassword(email: string) {
  const { data } = await apiClient.post<{ message: string }>("/auth/forgot-password", { email });
  return data;
}

// Full logout: invalidates the httpOnly cookie server-side, clears local
// session state and empties the react-query cache so no data from the
// previous user leaks into the next session.
export async function performLogout(): Promise<void> {
  try {
    await apiClient.post("/auth/logout");
  } catch {
    // Best-effort: clear local state even if the request fails
  }
  useSessionStore.getState().logout();
  queryClient.clear();
}

export async function resetPassword(token: string, new_password: string) {
  const { data } = await apiClient.post<{ message: string }>("/auth/reset-password", { token, new_password });
  return data;
}

export function useEmailConfig() {
  return useQuery({
    queryKey: ["email-config"],
    queryFn: async () => {
      const { data } = await apiClient.get<{ configured: boolean; from_email: string | null; from_name: string | null; public_base_url: string | null }>("/auth/email-config");
      return data;
    },
  });
}

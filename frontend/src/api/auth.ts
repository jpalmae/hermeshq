import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";
import { resolveApiBase } from "../lib/apiBase";
import type { AuthProvidersResponse, User } from "../types/api";

export async function login(username: string, password: string) {
  const { data } = await apiClient.post<{ access_token: string; expires_at: string }>(
    "/auth/login",
    { username, password },
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
      theme_preference?: "default" | "dark" | "light" | "system" | "enterprise" | "sixmanager";
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

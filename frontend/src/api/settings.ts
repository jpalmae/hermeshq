import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { AppSettings } from "../types/api";

const apiBase = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api";
const apiRoot = apiBase.replace(/\/api$/, "");

export function resolveAssetUrl(path: string | null | undefined) {
  if (!path) {
    return null;
  }
  if (/^https?:\/\//.test(path)) {
    return path;
  }
  return `${apiRoot}${path.startsWith("/") ? path : `/${path}`}`;
}

export function useSettings(enabled = true) {
  return useQuery({
    queryKey: ["settings"],
    queryFn: async () => {
      const { data } = await apiClient.get<AppSettings>("/settings");
      return data;
    },
    enabled,
  });
}

export function usePublicBranding() {
  return useQuery({
    queryKey: ["branding", "public"],
    queryFn: async () => {
      const { data } = await apiClient.get<AppSettings>("/settings/public");
      return data;
    },
    staleTime: 30_000,
  });
}

export function useUpdateSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      const { data } = await apiClient.put<AppSettings>("/settings", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
      await queryClient.invalidateQueries({ queryKey: ["branding", "public"] });
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}

export function useUploadBrandAsset(kind: "logo" | "favicon") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (file: File) => {
      const formData = new FormData();
      formData.append("file", file);
      const { data } = await apiClient.post<AppSettings>(`/settings/branding/${kind}`, formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
      await queryClient.invalidateQueries({ queryKey: ["branding", "public"] });
    },
  });
}

export function useDeleteBrandAsset(kind: "logo" | "favicon") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data } = await apiClient.delete<AppSettings>(`/settings/branding/${kind}`);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
      await queryClient.invalidateQueries({ queryKey: ["branding", "public"] });
    },
  });
}

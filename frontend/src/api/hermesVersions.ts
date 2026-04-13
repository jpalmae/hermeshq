import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { HermesVersion } from "../types/api";

export function useHermesVersions(enabled = true) {
  return useQuery({
    queryKey: ["hermes-versions"],
    queryFn: async () => {
      const { data } = await apiClient.get<HermesVersion[]>("/hermes-versions");
      return data;
    },
    enabled,
  });
}

export function useInstallHermesVersion() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (version: string) => {
      const { data } = await apiClient.post<HermesVersion>(`/hermes-versions/${version}/install`);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["hermes-versions"] });
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}

export function useUninstallHermesVersion() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (version: string) => {
      await apiClient.delete(`/hermes-versions/${version}`);
      return version;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["hermes-versions"] });
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}

export function useCreateHermesVersion() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: { version: string; release_tag?: string | null; description?: string | null }) => {
      const { data } = await apiClient.post<HermesVersion>("/hermes-versions", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["hermes-versions"] });
    },
  });
}

export function useUpdateHermesVersion() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      version,
      payload,
    }: {
      version: string;
      payload: { release_tag?: string | null; description?: string | null };
    }) => {
      const { data } = await apiClient.put<HermesVersion>(`/hermes-versions/${version}`, payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["hermes-versions"] });
    },
  });
}

export function useDeleteHermesVersionCatalogEntry() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (version: string) => {
      await apiClient.delete(`/hermes-versions/${version}/catalog`);
      return version;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["hermes-versions"] });
    },
  });
}

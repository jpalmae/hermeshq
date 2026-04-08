import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { ManagedIntegrationDefinition } from "../types/api";

export function useIntegrationPackages(enabled = true) {
  return useQuery({
    queryKey: ["integration-packages"],
    queryFn: async () => {
      const { data } = await apiClient.get<ManagedIntegrationDefinition[]>("/integration-packages");
      return data;
    },
    enabled,
  });
}

export function useUploadIntegrationPackage() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (file: File) => {
      const formData = new FormData();
      formData.append("file", file);
      const { data } = await apiClient.post<ManagedIntegrationDefinition>("/integration-packages/upload", formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["integration-packages"] });
      await queryClient.invalidateQueries({ queryKey: ["managed-integrations"] });
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}

export function useInstallIntegrationPackage() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (slug: string) => {
      const { data } = await apiClient.post<ManagedIntegrationDefinition>(`/integration-packages/${slug}/install`);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["integration-packages"] });
      await queryClient.invalidateQueries({ queryKey: ["managed-integrations"] });
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}

export function useUninstallIntegrationPackage() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (slug: string) => {
      await apiClient.post(`/integration-packages/${slug}/uninstall`);
      return slug;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["integration-packages"] });
      await queryClient.invalidateQueries({ queryKey: ["managed-integrations"] });
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
  });
}

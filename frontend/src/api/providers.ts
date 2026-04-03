import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { ProviderDefinition } from "../types/api";

export function useProviders(enabled = true) {
  return useQuery({
    queryKey: ["providers"],
    queryFn: async () => {
      const { data } = await apiClient.get<ProviderDefinition[]>("/providers");
      return data;
    },
    enabled,
  });
}

export function useUpdateProvider() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      providerSlug,
      payload,
    }: {
      providerSlug: string;
      payload: Record<string, unknown>;
    }) => {
      const { data } = await apiClient.put<ProviderDefinition>(`/providers/${providerSlug}`, payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["providers"] });
      await queryClient.invalidateQueries({ queryKey: ["settings"] });
    },
  });
}

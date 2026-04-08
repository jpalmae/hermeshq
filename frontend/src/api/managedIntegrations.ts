import { useQuery } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { ManagedIntegrationDefinition } from "../types/api";

export function useManagedIntegrations(enabled = true) {
  return useQuery({
    queryKey: ["managed-integrations"],
    queryFn: async () => {
      const { data } = await apiClient.get<ManagedIntegrationDefinition[]>("/managed-integrations");
      return data;
    },
    enabled,
  });
}

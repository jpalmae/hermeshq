import { useQuery } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { RuntimeCapabilityOverview, RuntimeProfileDefinition } from "../types/api";

export function useRuntimeProfiles(enabled = true) {
  return useQuery({
    queryKey: ["runtime-profiles"],
    queryFn: async () => {
      const { data } = await apiClient.get<RuntimeProfileDefinition[]>("/runtime-profiles");
      return data;
    },
    enabled,
  });
}

export function useRuntimeCapabilityOverview(enabled = true) {
  return useQuery({
    queryKey: ["runtime-capability-overview"],
    queryFn: async () => {
      const { data } = await apiClient.get<RuntimeCapabilityOverview>("/runtime-profiles/overview");
      return data;
    },
    enabled,
  });
}

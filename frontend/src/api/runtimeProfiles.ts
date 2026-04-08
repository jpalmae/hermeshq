import { useQuery } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { RuntimeProfileDefinition } from "../types/api";

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


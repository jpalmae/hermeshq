import { useQuery } from "@tanstack/react-query";

import { apiClient } from "./client";

export function useLogs(agentId?: string) {
  return useQuery({
    queryKey: ["logs", agentId ?? "all"],
    queryFn: async () => {
      const { data } = await apiClient.get("/logs", {
        params: agentId ? { agent_id: agentId } : undefined,
      });
      return data as Array<Record<string, unknown>>;
    },
    refetchInterval: 5000,
  });
}


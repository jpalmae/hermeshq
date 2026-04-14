import { useQuery } from "@tanstack/react-query";

import type { TerminalSession } from "../types/api";
import { apiClient } from "./client";

export function useTerminalSessions(agentId?: string) {
  return useQuery({
    queryKey: ["terminal-sessions", agentId ?? "all"],
    queryFn: async () => {
      const { data } = await apiClient.get("/terminal-sessions", {
        params: agentId ? { agent_id: agentId } : undefined,
      });
      return data as TerminalSession[];
    },
    refetchInterval: 5000,
  });
}

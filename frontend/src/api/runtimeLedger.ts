import { useQuery } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { RuntimeLedgerEntry } from "../types/api";

export function useRuntimeLedger(agentId?: string) {
  return useQuery({
    queryKey: ["runtime-ledger", agentId ?? "none"],
    queryFn: async () => {
      const { data } = await apiClient.get<{ entries: RuntimeLedgerEntry[] }>("/runtime-ledger", {
        params: { agent_id: agentId },
      });
      return data.entries;
    },
    enabled: Boolean(agentId),
    refetchInterval: 5000,
  });
}

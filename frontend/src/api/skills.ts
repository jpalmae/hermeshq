import { useQuery } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { AgentSkillState, SkillCatalogResponse } from "../types/api";

export function useSkillCatalog(query: string, limit = 12) {
  const normalized = query.trim();
  return useQuery({
    queryKey: ["skills", "catalog", normalized, limit],
    queryFn: async () => {
      const { data } = await apiClient.get<SkillCatalogResponse>("/skills/catalog", {
        params: { q: normalized, limit },
      });
      return data;
    },
    enabled: normalized.length > 0,
  });
}

export function useAgentSkills(agentId: string | undefined) {
  return useQuery({
    queryKey: ["skills", "agent", agentId],
    queryFn: async () => {
      const { data } = await apiClient.get<AgentSkillState>(`/skills/agents/${agentId}`);
      return data;
    },
    enabled: Boolean(agentId),
  });
}

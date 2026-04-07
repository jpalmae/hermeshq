import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

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

export function useDeleteInstalledSkill() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ agentId, path }: { agentId: string; path: string }) => {
      const { data } = await apiClient.delete<AgentSkillState>(`/skills/agents/${agentId}/installed`, {
        params: { path },
      });
      return data;
    },
    onSuccess: async (_, variables) => {
      await queryClient.invalidateQueries({ queryKey: ["skills", "agent", variables.agentId] });
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["agents", variables.agentId] });
    },
  });
}

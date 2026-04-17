import { useInfiniteQuery } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { ActivityLogPage } from "../types/api";

export function useLogs(agentId?: string, limit = 100, query = "") {
  const normalizedQuery = query.trim();
  return useInfiniteQuery({
    queryKey: ["logs", agentId ?? "all", limit, normalizedQuery],
    initialPageParam: null as { before_created_at: string; before_id: string } | null,
    queryFn: async ({ pageParam }) => {
      const params: Record<string, string | number> = { limit };
      if (agentId) {
        params.agent_id = agentId;
      }
      if (normalizedQuery) {
        params.query = normalizedQuery;
      }
      if (pageParam?.before_created_at) {
        params.before_created_at = pageParam.before_created_at;
      }
      if (pageParam?.before_id) {
        params.before_id = pageParam.before_id;
      }
      const { data } = await apiClient.get<ActivityLogPage>("/logs", { params });
      return data;
    },
    getNextPageParam: (lastPage) => {
      if (!lastPage.has_more || !lastPage.next_before_created_at || !lastPage.next_before_id) {
        return undefined;
      }
      return {
        before_created_at: lastPage.next_before_created_at,
        before_id: lastPage.next_before_id,
      };
    },
  });
}

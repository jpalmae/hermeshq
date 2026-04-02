import { useQuery } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { DashboardOverview } from "../types/api";

export function useDashboardOverview() {
  return useQuery({
    queryKey: ["dashboard", "overview"],
    queryFn: async () => {
      const { data } = await apiClient.get<DashboardOverview>("/dashboard/overview");
      return data;
    },
    refetchInterval: 5000,
  });
}


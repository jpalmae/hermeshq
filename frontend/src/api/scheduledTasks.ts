import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";

export function useScheduledTasks() {
  return useQuery({
    queryKey: ["scheduled-tasks"],
    queryFn: async () => {
      const { data } = await apiClient.get("/scheduled-tasks");
      return data as Array<Record<string, unknown>>;
    },
    refetchInterval: 10000,
  });
}

export function useCreateScheduledTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      const { data } = await apiClient.post("/scheduled-tasks", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["scheduled-tasks"] });
    },
  });
}


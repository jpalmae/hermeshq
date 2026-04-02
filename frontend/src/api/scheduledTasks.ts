import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { ScheduledTask } from "../types/api";

export function useScheduledTasks() {
  return useQuery({
    queryKey: ["scheduled-tasks"],
    queryFn: async () => {
      const { data } = await apiClient.get("/scheduled-tasks");
      return data as ScheduledTask[];
    },
    refetchInterval: 10000,
  });
}

export function useCreateScheduledTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      const { data } = await apiClient.post<ScheduledTask>("/scheduled-tasks", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["scheduled-tasks"] });
    },
  });
}

export function useDeleteScheduledTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (scheduledTaskId: string) => {
      await apiClient.delete(`/scheduled-tasks/${scheduledTaskId}`);
      return scheduledTaskId;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["scheduled-tasks"] });
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

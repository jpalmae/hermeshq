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

export function useUpdateScheduledTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, ...payload }: Record<string, unknown> & { id: string }) => {
      const { data } = await apiClient.put<ScheduledTask>(`/scheduled-tasks/${id}`, payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["scheduled-tasks"] });
    },
  });
}

export function useRunScheduledTaskNow() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (scheduledTaskId: string) => {
      const { data } = await apiClient.post<ScheduledTask>(`/scheduled-tasks/${scheduledTaskId}/run`);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["scheduled-tasks"] });
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

export function useApproveScheduledTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (schedule: Pick<ScheduledTask, "id" | "cron_expression" | "prompt">) => {
      const { data } = await apiClient.post<ScheduledTask>(`/scheduled-tasks/${schedule.id}/approve`, {
        cron_expression: schedule.cron_expression,
        prompt: schedule.prompt,
      });
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["scheduled-tasks"] });
    },
  });
}

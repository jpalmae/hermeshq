import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { Task, TaskBoardUpdate } from "../types/api";

export function useTasks(agentId?: string) {
  return useQuery({
    queryKey: ["tasks", agentId ?? "all"],
    queryFn: async () => {
      const { data } = await apiClient.get<Task[]>("/tasks", {
        params: agentId ? { agent_id: agentId } : undefined,
      });
      return data;
    },
    refetchInterval: 5000,
  });
}

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

function needsMessageDetails(task: Task): boolean {
  if (task.messages_json || task.response?.trim()) {
    return false;
  }
  return task.status === "running" || TERMINAL_STATUSES.has(task.status);
}

export function useConversationTaskDetails(tasks: Task[]): Record<string, Task> {
  const pending = tasks.filter(needsMessageDetails);
  return useQueries({
    queries: pending.map((task) => {
      const isRunning = task.status === "running";
      return {
        queryKey: ["task", task.id] as const,
        queryFn: async () => {
          const { data } = await apiClient.get<Task>(`/tasks/${task.id}`);
          return data;
        },
        staleTime: isRunning ? 0 : Infinity,
        refetchInterval: isRunning ? 2000 : false,
      };
    }),
    combine: (results) => {
      const details: Record<string, Task> = {};
      for (const result of results) {
        if (result.data) {
          details[result.data.id] = result.data;
        }
      }
      return details;
    },
  });
}

export function useCreateTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      const { data } = await apiClient.post<Task>("/tasks", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });
}

export function useCancelTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (taskId: string) => {
      const { data } = await apiClient.post<Task>(`/tasks/${taskId}/cancel`);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useDeleteTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (taskId: string) => {
      await apiClient.delete(`/tasks/${taskId}`);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useUpdateTaskBoard() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ taskId, payload }: { taskId: string; payload: TaskBoardUpdate }) => {
      const { data } = await apiClient.patch<Task>(`/tasks/${taskId}/board`, payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

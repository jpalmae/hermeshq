import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import axios from "axios";

import { apiClient } from "./client";
import type { Agent, BulkAgentOperationResult } from "../types/api";

export function useAgents(includeArchived = false) {
  return useQuery({
    queryKey: ["agents", { includeArchived }],
    queryFn: async () => {
      const { data } = await apiClient.get<Agent[]>("/agents", {
        params: includeArchived ? { include_archived: true } : undefined,
      });
      return data;
    },
  });
}

export function useAgent(agentId: string | undefined) {
  return useQuery({
    queryKey: ["agents", agentId],
    queryFn: async () => {
      try {
        const { data } = await apiClient.get<Agent>(`/agents/${agentId}`);
        return data;
      } catch (error) {
        if (axios.isAxiosError(error) && (error.response?.status === 403 || error.response?.status === 404 || !error.response)) {
          return null;
        }
        throw error;
      }
    },
    enabled: Boolean(agentId),
    retry: false,
  });
}

export function useCreateAgent() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      const { data } = await apiClient.post<Agent>("/agents", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useBootstrapSystemOperator() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const { data } = await apiClient.post<Agent>("/agents/system/operator/bootstrap");
      return data;
    },
    onSuccess: async (agent) => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["agents", agent.id] });
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useUpdateAgent() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ agentId, payload }: { agentId: string; payload: Record<string, unknown> }) => {
      const { data } = await apiClient.put<Agent>(`/agents/${agentId}`, payload);
      return data;
    },
    onSuccess: async (_, variables) => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["agents", variables.agentId] });
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      await queryClient.invalidateQueries({ queryKey: ["skills", "agent", variables.agentId] });
    },
  });
}

export function useDeleteAgent() {
  const queryClient = useQueryClient();
  return useMutation({
    onMutate: async (agentId) => {
      await queryClient.cancelQueries({ queryKey: ["agents"] });
      await queryClient.cancelQueries({ queryKey: ["agents", agentId] });

      const previousAgents = queryClient.getQueryData<Agent[]>(["agents"]) ?? [];
      const removedAgent = previousAgents.find((agent) => agent.id === agentId) ?? null;

      queryClient.setQueryData<Agent[]>(
        ["agents"],
        previousAgents.filter((agent) => agent.id !== agentId),
      );
      queryClient.setQueryData(["agents", agentId], null);

      return { previousAgents, removedAgent };
    },
    mutationFn: async (agentId: string) => {
      try {
        await apiClient.delete(`/agents/${agentId}`);
      } catch (error) {
        if (axios.isAxiosError(error) && (error.response?.status === 404 || !error.response)) {
          return agentId;
        }
        throw error;
      }
      return agentId;
    },
    onSuccess: async (agentId) => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["agents", agentId] });
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
      await queryClient.invalidateQueries({ queryKey: ["logs"] });
    },
    onSettled: async (_, __, agentId) => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["agents", agentId] });
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
      await queryClient.invalidateQueries({ queryKey: ["logs"] });
    },
  });
}

export function useAgentAction(path: "start" | "stop" | "restart") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (agentId: string) => {
      const { data } = await apiClient.post<Agent>(`/agents/${agentId}/${path}`);
      return data;
    },
    onSuccess: async (_, agentId) => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["agents", agentId] });
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

export function useBulkAgentTask() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: {
      agent_ids: string[];
      title: string;
      prompt: string;
      priority: number;
      auto_start_stopped: boolean;
    }) => {
      const { data } = await apiClient.post<BulkAgentOperationResult>("/agents/bulk/task", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      await queryClient.invalidateQueries({ queryKey: ["logs"] });
    },
  });
}

export function useBulkAgentMessage() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: {
      agent_ids: string[];
      message: string;
      auto_start_stopped: boolean;
    }) => {
      const { data } = await apiClient.post<BulkAgentOperationResult>("/agents/bulk/message", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
      await queryClient.invalidateQueries({ queryKey: ["logs"] });
    },
  });
}

export function useUploadAgentAvatar() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ agentId, file }: { agentId: string; file: File }) => {
      const formData = new FormData();
      formData.append("file", file);
      const { data } = await apiClient.post<Agent>(`/agents/${agentId}/avatar`, formData, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      return data;
    },
    onSuccess: async (_, variables) => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["agents", variables.agentId] });
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useDeleteAgentAvatar() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (agentId: string) => {
      const { data } = await apiClient.delete<Agent>(`/agents/${agentId}/avatar`);
      return data;
    },
    onSuccess: async (_, agentId) => {
      await queryClient.invalidateQueries({ queryKey: ["agents"] });
      await queryClient.invalidateQueries({ queryKey: ["agents", agentId] });
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export function useTestAgentIntegration() {
  return useMutation({
    mutationFn: async ({
      agentId,
      integrationSlug,
      config,
    }: {
      agentId: string;
      integrationSlug: string;
      config: Record<string, string>;
    }) => {
      const { data } = await apiClient.post<{ success: boolean; message: string; details: Record<string, unknown> | null }>(
        `/agents/${agentId}/integrations/${integrationSlug}/test`,
        { config },
      );
      return data;
    },
  });
}

export function useRunAgentIntegrationAction() {
  return useMutation({
    mutationFn: async ({
      agentId,
      integrationSlug,
      actionSlug,
      config,
    }: {
      agentId: string;
      integrationSlug: string;
      actionSlug: string;
      config: Record<string, string>;
    }) => {
      const { data } = await apiClient.post<{ success: boolean; message: string; details: Record<string, unknown> | null }>(
        `/agents/${agentId}/integrations/${integrationSlug}/actions/${actionSlug}`,
        { config },
      );
      return data;
    },
  });
}

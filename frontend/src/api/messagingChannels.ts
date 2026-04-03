import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { MessagingChannel, MessagingChannelRuntime } from "../types/api";

export function useMessagingChannel(agentId: string | undefined, platform: string, enabled = true) {
  return useQuery({
    queryKey: ["messaging-channels", agentId, platform],
    queryFn: async () => {
      const { data } = await apiClient.get<MessagingChannel>(`/agents/${agentId}/channels/${platform}`);
      return data;
    },
    enabled: enabled && Boolean(agentId),
  });
}

export function useMessagingChannelRuntime(agentId: string | undefined, platform: string, enabled = true) {
  return useQuery({
    queryKey: ["messaging-channels", agentId, platform, "runtime"],
    queryFn: async () => {
      const { data } = await apiClient.get<MessagingChannelRuntime>(`/agents/${agentId}/channels/${platform}/runtime`);
      return data;
    },
    enabled: enabled && Boolean(agentId),
    refetchInterval: 5000,
  });
}

export function useMessagingChannelLogs(agentId: string | undefined, platform: string, enabled = true) {
  return useQuery({
    queryKey: ["messaging-channels", agentId, platform, "logs"],
    queryFn: async () => {
      const { data } = await apiClient.get<{ platform: string; content: string }>(`/agents/${agentId}/channels/${platform}/logs`);
      return data.content;
    },
    enabled: enabled && Boolean(agentId),
    refetchInterval: 5000,
  });
}

export function useUpdateMessagingChannel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      agentId,
      platform,
      payload,
    }: {
      agentId: string;
      platform: string;
      payload: Record<string, unknown>;
    }) => {
      const { data } = await apiClient.put<MessagingChannel>(`/agents/${agentId}/channels/${platform}`, payload);
      return data;
    },
    onSuccess: async (_, variables) => {
      await queryClient.invalidateQueries({ queryKey: ["messaging-channels", variables.agentId, variables.platform] });
      await queryClient.invalidateQueries({ queryKey: ["messaging-channels", variables.agentId, variables.platform, "runtime"] });
      await queryClient.invalidateQueries({ queryKey: ["messaging-channels", variables.agentId, variables.platform, "logs"] });
    },
  });
}

export function useMessagingChannelAction(action: "start" | "stop") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ agentId, platform }: { agentId: string; platform: string }) => {
      const { data } = await apiClient.post<MessagingChannelRuntime>(`/agents/${agentId}/channels/${platform}/${action}`);
      return data;
    },
    onSuccess: async (_, variables) => {
      await queryClient.invalidateQueries({ queryKey: ["messaging-channels", variables.agentId, variables.platform] });
      await queryClient.invalidateQueries({ queryKey: ["messaging-channels", variables.agentId, variables.platform, "runtime"] });
      await queryClient.invalidateQueries({ queryKey: ["messaging-channels", variables.agentId, variables.platform, "logs"] });
    },
  });
}

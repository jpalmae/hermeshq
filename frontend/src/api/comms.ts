import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";

export interface MessagePayload {
  from_agent_id: string;
  to_agent_id: string;
  message_type?: string;
  content: string;
  metadata?: Record<string, unknown>;
}

export interface BroadcastPayload {
  from_agent_id: string;
  team_tag: string;
  content: string;
  metadata?: Record<string, unknown>;
}

export interface CommsMessage {
  id: string;
  from_agent_id: string;
  to_agent_id: string;
  task_id: string | null;
  message_type: string;
  content: string;
  metadata: Record<string, unknown>;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface TopologyNode {
  id: string;
  label: string;
  slug: string;
  status: string;
}

export interface TopologyEdge {
  id: string;
  source: string;
  target: string;
  type: string;
}

export function useCommsHistory() {
  return useQuery({
    queryKey: ["comms", "history"],
    queryFn: async () => {
      const { data } = await apiClient.get("/comms/history");
      return data as CommsMessage[];
    },
    refetchInterval: 5000,
  });
}

export function useCommsTopology() {
  return useQuery({
    queryKey: ["comms", "topology"],
    queryFn: async () => {
      const { data } = await apiClient.get("/comms/topology");
      return data as { nodes: TopologyNode[]; edges: TopologyEdge[] };
    },
    refetchInterval: 5000,
  });
}

export function useSendMessage() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: MessagePayload) => {
      const { data } = await apiClient.post("/comms/send", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["comms"] });
      await queryClient.invalidateQueries({ queryKey: ["tasks"] });
    },
  });
}

export function useBroadcast() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: BroadcastPayload) => {
      const { data } = await apiClient.post("/comms/broadcast", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["comms"] });
    },
  });
}

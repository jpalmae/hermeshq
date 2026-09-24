import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";

export interface EnrolledDevice {
  id: string;
  agent_id: string;
  user_id: string;
  name: string;
  os_info: Record<string, unknown>;
  status: "pending" | "active" | "revoked" | "stale";
  token_version: number;
  guard_fail_mode: string;
  last_heartbeat: string | null;
  last_sync_at: string | null;
  bundle_etag: string | null;
  hermes_version: string | null;
  revoked_at: string | null;
  created_at: string | null;
}

export interface EnrollResponse extends EnrolledDevice {
  enroll_token: string;
}

export function useEnrolledDevices(agentId: string) {
  return useQuery<EnrolledDevice[]>({
    queryKey: ["enrollment", "devices", agentId],
    queryFn: async () => {
      const { data } = await apiClient.get<EnrolledDevice[]>(`/enrollment/devices`, {
        params: { agent_id: agentId },
      });
      return data;
    },
  });
}

export function useEnrollDevice(agentId: string) {
  const queryClient = useQueryClient();
  return useMutation<EnrollResponse, Error, { device_name: string; guard_fail_mode: string }>({
    mutationFn: async (payload) => {
      const { data } = await apiClient.post<EnrollResponse>("/enrollment/enroll", {
        agent_id: agentId,
        ...payload,
      });
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["enrollment", "devices", agentId] });
    },
  });
}

export function useRevokeDevice(agentId: string) {
  const queryClient = useQueryClient();
  return useMutation<EnrolledDevice, Error, string>({
    mutationFn: async (deviceId) => {
      const { data } = await apiClient.delete<EnrolledDevice>(`/enrollment/devices/${deviceId}`);
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["enrollment", "devices", agentId] });
    },
  });
}

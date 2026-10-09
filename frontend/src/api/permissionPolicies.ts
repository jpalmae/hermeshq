import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";

export interface PermissionPolicy {
  id: string;
  name: string;
  description: string | null;
  tool_rules: {
    allow?: string[];
    deny?: string[];
  };
  path_rules: {
    allow_paths?: string[];
    deny_paths?: string[];
  };
  command_rules: {
    allow?: string[];
    deny?: string[];
  };
  network_rules: {
    allow_domains?: string[];
    deny_all?: boolean;
  };
  approval_rules: {
    require_approval_for?: string[];
    auto_approve_threshold?: string;
  };
  is_system: boolean;
  created_at: string;
  updated_at: string;
}

export interface PermissionPolicyCreate {
  name: string;
  description?: string;
  tool_rules?: Record<string, string[]>;
  path_rules?: Record<string, string[]>;
  command_rules?: Record<string, string[]>;
  network_rules?: Record<string, unknown>;
  approval_rules?: Record<string, unknown>;
}

export interface PermissionTestResult {
  allowed: boolean;
  reason: string | null;
  policy_name: string | null;
  requires_approval: boolean;
}

export function usePermissionPolicies() {
  return useQuery({
    queryKey: ["permission-policies"],
    queryFn: async () => {
      const { data } = await apiClient.get<PermissionPolicy[]>("/permission-policies");
      return data;
    },
  });
}

export function useCreatePermissionPolicy() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (payload: PermissionPolicyCreate) => {
      const { data } = await apiClient.post<PermissionPolicy>("/permission-policies", payload);
      return data;
    },
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["permission-policies"] });
    },
  });
}

export function useUpdatePermissionPolicy() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async ({ id, payload }: { id: string; payload: Partial<PermissionPolicyCreate> }) => {
      const { data } = await apiClient.put<PermissionPolicy>(`/permission-policies/${id}`, payload);
      return data;
    },
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["permission-policies"] });
    },
  });
}

export function useDeletePermissionPolicy() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      await apiClient.delete(`/permission-policies/${id}`);
    },
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["permission-policies"] });
    },
  });
}

export function useTestPermission() {
  return useMutation({
    mutationFn: async ({ agentId, tool, input }: { agentId: string; tool: string; input: Record<string, unknown> }) => {
      const { data } = await apiClient.post<PermissionTestResult>(
        `/agents/${agentId}/test-permission`,
        { tool, input },
      );
      return data;
    },
  });
}

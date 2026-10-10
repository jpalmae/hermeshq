import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";

export interface CloudPlatform {
  id: string;
  kind: string;
  name: string;
  api_url: string;
  admin_credential_ref: string | null;
  insecure_tls: boolean;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface CloudTenant {
  id: string;
  platform_id: string;
  tenant_ref: string;
  display_name: string;
  credential_ref: string;
  cache_ttl_seconds: number;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface CloudBinding {
  id: string;
  tenant_id: string;
  agent_id: string;
  cloud_role: string;
  created_at: string;
  updated_at: string;
}

export function useCloudPlatforms(enabled = true) {
  return useQuery({
    queryKey: ["cloud", "platforms"],
    queryFn: async () => {
      const { data } = await apiClient.get<CloudPlatform[]>("/cloud/platforms");
      return data;
    },
    enabled,
  });
}

export function useCreateCloudPlatform() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      const { data } = await apiClient.post<CloudPlatform>("/cloud/platforms", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cloud", "platforms"] });
    },
  });
}

export function useDeleteCloudPlatform() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (platformId: string) => {
      await apiClient.delete(`/cloud/platforms/${platformId}`);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cloud", "platforms"] });
      await queryClient.invalidateQueries({ queryKey: ["cloud", "tenants"] });
    },
  });
}

export function useCloudTenants(enabled = true) {
  return useQuery({
    queryKey: ["cloud", "tenants"],
    queryFn: async () => {
      const { data } = await apiClient.get<CloudTenant[]>("/cloud/tenants");
      return data;
    },
    enabled,
  });
}

export function useCreateCloudTenant() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      const { data } = await apiClient.post<CloudTenant>("/cloud/tenants", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cloud", "tenants"] });
    },
  });
}

export function useDeleteCloudTenant() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (tenantId: string) => {
      await apiClient.delete(`/cloud/tenants/${tenantId}`);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cloud", "tenants"] });
      await queryClient.invalidateQueries({ queryKey: ["cloud", "bindings"] });
    },
  });
}

export function useCloudBindings(enabled = true) {
  return useQuery({
    queryKey: ["cloud", "bindings"],
    queryFn: async () => {
      const { data } = await apiClient.get<CloudBinding[]>("/cloud/bindings");
      return data;
    },
    enabled,
  });
}

export function useCreateCloudBinding() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      const { data } = await apiClient.post<CloudBinding>("/cloud/bindings", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cloud", "bindings"] });
    },
  });
}

export function useDeleteCloudBinding() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (bindingId: string) => {
      await apiClient.delete(`/cloud/bindings/${bindingId}`);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["cloud", "bindings"] });
    },
  });
}

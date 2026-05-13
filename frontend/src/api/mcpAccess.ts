import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { McpAccessToken, McpAccessTokenCreateResult } from "../types/api";

export function useMcpAccessTokens(enabled = true) {
  return useQuery({
    queryKey: ["mcp-access-tokens"],
    queryFn: async () => {
      const { data } = await apiClient.get<McpAccessToken[]>("/mcp-access/access-tokens");
      return data;
    },
    enabled,
  });
}

export function useCreateMcpAccessToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      const { data } = await apiClient.post<McpAccessTokenCreateResult>("/mcp-access/access-tokens", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["mcp-access-tokens"] });
      await queryClient.invalidateQueries({ queryKey: ["logs"] });
    },
  });
}

export function useUpdateMcpAccessToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ tokenId, payload }: { tokenId: string; payload: Record<string, unknown> }) => {
      const { data } = await apiClient.patch<McpAccessToken>(`/mcp-access/access-tokens/${tokenId}`, payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["mcp-access-tokens"] });
      await queryClient.invalidateQueries({ queryKey: ["logs"] });
    },
  });
}

export function useRevokeMcpAccessToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (tokenId: string) => {
      await apiClient.delete(`/mcp-access/access-tokens/${tokenId}`);
      return tokenId;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["mcp-access-tokens"] });
      await queryClient.invalidateQueries({ queryKey: ["logs"] });
    },
  });
}

export function useRotateMcpAccessToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (tokenId: string) => {
      const { data } = await apiClient.post<{ token: string; access: McpAccessToken }>(
        `/mcp-access/access-tokens/${tokenId}/rotate`,
      );
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["mcp-access-tokens"] });
      await queryClient.invalidateQueries({ queryKey: ["logs"] });
    },
  });
}

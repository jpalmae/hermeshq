import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";

export function useWorkspace(agentId: string | undefined, path = ".") {
  return useQuery({
    queryKey: ["workspace", agentId, path],
    queryFn: async () => {
      const { data } = await apiClient.get(`/agents/${agentId}/workspace`, { params: { path } });
      return data as {
        entries: Array<{ name: string; path: string; is_dir: boolean; size: number }>;
        size: number;
      };
    },
    enabled: Boolean(agentId),
  });
}

export function useWorkspaceFile(agentId: string | undefined, filePath: string | null) {
  return useQuery({
    queryKey: ["workspace-file", agentId, filePath],
    queryFn: async () => {
      const { data } = await apiClient.get(`/agents/${agentId}/workspace/${filePath}`);
      return data as { path: string; content: string };
    },
    enabled: Boolean(agentId && filePath),
  });
}

export function useWriteWorkspaceFile(agentId: string | undefined, path = ".") {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ filePath, content }: { filePath: string; content: string }) => {
      const { data } = await apiClient.put(`/agents/${agentId}/workspace/${filePath}`, { content });
      return data;
    },
    onSuccess: async (_, variables) => {
      await queryClient.invalidateQueries({ queryKey: ["workspace", agentId, path] });
      await queryClient.invalidateQueries({ queryKey: ["workspace-file", agentId, variables.filePath] });
    },
  });
}


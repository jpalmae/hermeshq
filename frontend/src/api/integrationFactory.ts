import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";
import type {
  IntegrationDraft,
  IntegrationDraftFileContent,
  IntegrationDraftPublishResult,
  IntegrationDraftValidation,
} from "../types/api";

export function useIntegrationDrafts(enabled = true) {
  return useQuery({
    queryKey: ["integration-drafts"],
    queryFn: async () => {
      const { data } = await apiClient.get<IntegrationDraft[]>("/integration-factory/drafts");
      return data;
    },
    enabled,
  });
}

export function useIntegrationDraftFile(draftId: string | null, path: string | null, enabled = true) {
  return useQuery({
    queryKey: ["integration-draft-file", draftId, path],
    queryFn: async () => {
      const { data } = await apiClient.get<IntegrationDraftFileContent>(`/integration-factory/drafts/${draftId}/file`, {
        params: { path },
      });
      return data;
    },
    enabled: Boolean(enabled && draftId && path),
  });
}

function invalidateFactoryQueries(queryClient: ReturnType<typeof useQueryClient>) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ["integration-drafts"] }),
    queryClient.invalidateQueries({ queryKey: ["integration-packages"] }),
    queryClient.invalidateQueries({ queryKey: ["managed-integrations"] }),
    queryClient.invalidateQueries({ queryKey: ["agents"] }),
    queryClient.invalidateQueries({ queryKey: ["skills"] }),
  ]);
}

export function useCreateIntegrationDraft() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: {
      slug: string;
      name: string;
      description?: string;
      template: "rest-api" | "empty";
      version?: string;
    }) => {
      const { data } = await apiClient.post<IntegrationDraft>("/integration-factory/drafts", payload);
      return data;
    },
    onSuccess: async () => {
      await invalidateFactoryQueries(queryClient);
    },
  });
}

export function useUpdateIntegrationDraft() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: {
      draftId: string;
      name?: string;
      description?: string;
      version?: string;
      notes?: string;
    }) => {
      const { draftId, ...body } = payload;
      const { data } = await apiClient.put<IntegrationDraft>(`/integration-factory/drafts/${draftId}`, body);
      return data;
    },
    onSuccess: async (_draft, variables) => {
      await invalidateFactoryQueries(queryClient);
      await queryClient.invalidateQueries({ queryKey: ["integration-draft-file", variables.draftId] });
    },
  });
}

export function useSaveIntegrationDraftFile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: {
      draftId: string;
      path: string;
      content: string;
    }) => {
      const { draftId, path, content } = payload;
      const { data } = await apiClient.put<IntegrationDraft>(
        `/integration-factory/drafts/${draftId}/file`,
        { content },
        { params: { path } },
      );
      return data;
    },
    onSuccess: async (_draft, variables) => {
      await invalidateFactoryQueries(queryClient);
      await queryClient.invalidateQueries({ queryKey: ["integration-draft-file", variables.draftId, variables.path] });
    },
  });
}

export function useDeleteIntegrationDraftFile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: { draftId: string; path: string }) => {
      const { draftId, path } = payload;
      const { data } = await apiClient.delete<IntegrationDraft>(`/integration-factory/drafts/${draftId}/file`, {
        params: { path },
      });
      return data;
    },
    onSuccess: async (_draft, variables) => {
      await invalidateFactoryQueries(queryClient);
      await queryClient.removeQueries({ queryKey: ["integration-draft-file", variables.draftId, variables.path] });
    },
  });
}

export function useValidateIntegrationDraft() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (draftId: string) => {
      const { data } = await apiClient.post<IntegrationDraftValidation>(`/integration-factory/drafts/${draftId}/validate`);
      return data;
    },
    onSuccess: async () => {
      await invalidateFactoryQueries(queryClient);
    },
  });
}

export function usePublishIntegrationDraft() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (draftId: string) => {
      const { data } = await apiClient.post<IntegrationDraftPublishResult>(`/integration-factory/drafts/${draftId}/publish`);
      return data;
    },
    onSuccess: async () => {
      await invalidateFactoryQueries(queryClient);
    },
  });
}

export function useDeleteIntegrationDraft() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (draftId: string) => {
      await apiClient.delete(`/integration-factory/drafts/${draftId}`);
      return draftId;
    },
    onSuccess: async (_draftId, variables) => {
      await invalidateFactoryQueries(queryClient);
      await queryClient.removeQueries({ queryKey: ["integration-draft-file", variables] });
    },
  });
}

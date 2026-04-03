import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";

export function useTemplates(enabled = true) {
  return useQuery({
    queryKey: ["templates"],
    queryFn: async () => {
      const { data } = await apiClient.get("/templates");
      return data as Array<Record<string, unknown>>;
    },
    enabled,
  });
}

export function useCreateTemplate() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      const { data } = await apiClient.post("/templates", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["templates"] });
    },
  });
}

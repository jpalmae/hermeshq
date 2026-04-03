import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { Secret } from "../types/api";

export function useSecrets(enabled = true) {
  return useQuery({
    queryKey: ["secrets"],
    queryFn: async () => {
      const { data } = await apiClient.get<Secret[]>("/secrets");
      return data;
    },
    enabled,
  });
}

export function useCreateSecret() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: Record<string, unknown>) => {
      const { data } = await apiClient.post("/secrets", payload);
      return data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["secrets"] });
    },
  });
}

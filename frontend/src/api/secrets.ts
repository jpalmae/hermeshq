import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";

export function useSecrets(enabled = true) {
  return useQuery({
    queryKey: ["secrets"],
    queryFn: async () => {
      const { data } = await apiClient.get("/secrets");
      return data as Array<Record<string, unknown>>;
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

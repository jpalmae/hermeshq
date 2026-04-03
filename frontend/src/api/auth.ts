import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { User } from "../types/api";

export async function login(username: string, password: string) {
  const { data } = await apiClient.post<{ access_token: string; expires_at: string }>(
    "/auth/login",
    { username, password },
  );
  return data;
}

export function useMe(enabled: boolean) {
  return useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      const { data } = await apiClient.get<User>("/auth/me");
      return data;
    },
    enabled,
    retry: false,
  });
}

export function useUpdateMyPreferences() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: { theme_preference: "default" | "dark" | "light" | "system" }) => {
      const { data } = await apiClient.put<User>("/auth/me/preferences", payload);
      return data;
    },
    onSuccess: async (data) => {
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      return data;
    },
  });
}

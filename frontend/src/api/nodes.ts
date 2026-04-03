import { useQuery } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { Node } from "../types/api";

export function useNodes(enabled = true) {
  return useQuery({
    queryKey: ["nodes"],
    queryFn: async () => {
      const { data } = await apiClient.get<Node[]>("/nodes");
      return data;
    },
    enabled,
  });
}

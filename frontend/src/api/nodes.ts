import { useQuery } from "@tanstack/react-query";

import { apiClient } from "./client";
import type { Node } from "../types/api";

export function useNodes() {
  return useQuery({
    queryKey: ["nodes"],
    queryFn: async () => {
      const { data } = await apiClient.get<Node[]>("/nodes");
      return data;
    },
  });
}


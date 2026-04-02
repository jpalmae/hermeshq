import { create } from "zustand";

import type { RealtimeEvent } from "../types/api";

interface RealtimeState {
  events: RealtimeEvent[];
  pushEvent: (event: RealtimeEvent) => void;
  clear: () => void;
}

export const useRealtimeStore = create<RealtimeState>((set) => ({
  events: [],
  pushEvent: (event) =>
    set((state) => ({
      events: [event, ...state.events].slice(0, 60),
    })),
  clear: () => set({ events: [] }),
}));


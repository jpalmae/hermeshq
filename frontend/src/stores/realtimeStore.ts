import { create } from "zustand";

import type { RealtimeEvent } from "../types/api";

const MAX_EVENTS = 60;
const PROGRESS_FLUSH_MS = 500;
const PROGRESS_MAX_CHARS = 400;

interface RealtimeState {
  events: RealtimeEvent[];
  pushEvent: (event: RealtimeEvent) => void;
  clear: () => void;
}

const pendingProgress = new Map<string, RealtimeEvent>();
let flushTimer: ReturnType<typeof setTimeout> | null = null;

export const useRealtimeStore = create<RealtimeState>((set) => {
  const flushProgress = () => {
    flushTimer = null;
    if (pendingProgress.size === 0) return;
    const batch = Array.from(pendingProgress.values()).reverse();
    pendingProgress.clear();
    set((state) => ({ events: [...batch, ...state.events].slice(0, MAX_EVENTS) }));
  };

  return {
    events: [],
    pushEvent: (event) => {
      if (event.type === "task.progress") {
        const key = event.task_id ?? event.agent_id ?? "";
        const previous = pendingProgress.get(key);
        const message = `${previous?.message ?? ""}${event.message ?? ""}`.slice(-PROGRESS_MAX_CHARS);
        pendingProgress.set(key, { ...event, message });
        if (flushTimer === null) flushTimer = setTimeout(flushProgress, PROGRESS_FLUSH_MS);
        return;
      }
      if (flushTimer !== null) clearTimeout(flushTimer);
      flushProgress();
      set((state) => ({ events: [event, ...state.events].slice(0, MAX_EVENTS) }));
    },
    clear: () => {
      pendingProgress.clear();
      if (flushTimer !== null) {
        clearTimeout(flushTimer);
        flushTimer = null;
      }
      set({ events: [] });
    },
  };
});

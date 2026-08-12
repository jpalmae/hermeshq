import { create } from "zustand";

import type { User } from "../types/api";

interface SessionState {
  token: string | null;
  user: User | null;
  setSession: (token: string, user: User | null) => void;
  setUser: (user: User | null) => void;
  setToken: (token: string) => void;
  logout: () => void;
}

export const useSessionStore = create<SessionState>((set) => ({
  token: null,
  user: null,
  setSession: (token, user) => {
    set({ token, user });
  },
  setUser: (user) => set({ user }),
  setToken: (token) => {
    set({ token });
  },
  logout: () => {
    set({ token: null, user: null });
  },
}));

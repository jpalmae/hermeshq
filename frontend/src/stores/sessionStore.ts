import { create } from "zustand";

import type { User } from "../types/api";

interface SessionState {
  token: string | null;
  user: User | null;
  setSession: (token: string, user: User | null) => void;
  setUser: (user: User | null) => void;
  logout: () => void;
}

const storedToken = window.localStorage.getItem("hermeshq.token");

export const useSessionStore = create<SessionState>((set) => ({
  token: storedToken,
  user: null,
  setSession: (token, user) => {
    window.localStorage.setItem("hermeshq.token", token);
    set({ token, user });
  },
  setUser: (user) => set({ user }),
  logout: () => {
    window.localStorage.removeItem("hermeshq.token");
    set({ token: null, user: null });
  },
}));


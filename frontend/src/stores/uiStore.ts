import { create } from "zustand";

interface UIState {
  sidebarCollapsed: boolean;
  mobileNavOpen: boolean;
  setSidebarCollapsed: (value: boolean) => void;
  toggleSidebar: () => void;
  setMobileNavOpen: (value: boolean) => void;
}

const storedSidebarState = window.localStorage.getItem("hermeshq.sidebarCollapsed");

export const useUIStore = create<UIState>((set) => ({
  sidebarCollapsed: storedSidebarState === "true",
  mobileNavOpen: false,
  setSidebarCollapsed: (value) => {
    window.localStorage.setItem("hermeshq.sidebarCollapsed", String(value));
    set({ sidebarCollapsed: value });
  },
  toggleSidebar: () =>
    set((state) => {
      const next = !state.sidebarCollapsed;
      window.localStorage.setItem("hermeshq.sidebarCollapsed", String(next));
      return { sidebarCollapsed: next };
    }),
  setMobileNavOpen: (value) => set({ mobileNavOpen: value }),
}));

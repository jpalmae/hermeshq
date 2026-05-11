import { create } from "zustand";

interface UIState {
  sidebarCollapsed: boolean;
  mobileNavOpen: boolean;
  setSidebarCollapsed: (value: boolean) => void;
  toggleSidebar: () => void;
  setMobileNavOpen: (value: boolean) => void;
}

function safeReadLocalStorage(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeWriteLocalStorage(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Silently ignore in private browsing mode (Safari, etc.)
  }
}

const storedSidebarState = safeReadLocalStorage("hermeshq.sidebarCollapsed");

export const useUIStore = create<UIState>((set) => ({
  sidebarCollapsed: storedSidebarState === "true",
  mobileNavOpen: false,
  setSidebarCollapsed: (value) => {
    safeWriteLocalStorage("hermeshq.sidebarCollapsed", String(value));
    set({ sidebarCollapsed: value });
  },
  toggleSidebar: () =>
    set((state) => {
      const next = !state.sidebarCollapsed;
      safeWriteLocalStorage("hermeshq.sidebarCollapsed", String(next));
      return { sidebarCollapsed: next };
    }),
  setMobileNavOpen: (value) => set({ mobileNavOpen: value }),
}));

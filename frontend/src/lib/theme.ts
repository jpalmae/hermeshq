import type { AppSettings } from "../types/api";

export type ThemeMode = AppSettings["theme_mode"];
export type ResolvedTheme = "dark" | "light";

export function resolveThemeMode(mode: ThemeMode | null | undefined): ResolvedTheme {
  if (mode === "light") {
    return "light";
  }
  if (mode === "system") {
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  return "dark";
}

export function applyThemeToDocument(mode: ThemeMode | null | undefined) {
  const resolvedTheme = resolveThemeMode(mode);
  document.documentElement.dataset.theme = resolvedTheme;
  document.documentElement.style.colorScheme = resolvedTheme;
  return resolvedTheme;
}


import type { AppSettings } from "../types/api";

export type ThemeMode = AppSettings["theme_mode"];
export type UserThemePreference = "default" | ThemeMode;
export type ResolvedTheme = "dark" | "light" | "enterprise";
const PUBLIC_THEME_STORAGE_KEY = "hermeshq.publicThemeMode";
const USER_THEME_STORAGE_KEY = "hermeshq.userThemeMode";

export function getStoredPublicThemeMode(): ThemeMode | null {
  const value = window.localStorage.getItem(PUBLIC_THEME_STORAGE_KEY);
  if (value === "dark" || value === "light" || value === "system" || value === "enterprise" || value === "sixmanager") {
    return value;
  }
  return null;
}

export function cachePublicThemeMode(mode: ThemeMode | null | undefined) {
  if (!mode) {
    return;
  }
  window.localStorage.setItem(PUBLIC_THEME_STORAGE_KEY, mode);
}

export function getStoredUserThemeMode(): ThemeMode | null {
  const value = window.localStorage.getItem(USER_THEME_STORAGE_KEY);
  if (value === "dark" || value === "light" || value === "system" || value === "enterprise" || value === "sixmanager") {
    return value;
  }
  return null;
}

export function cacheUserThemeMode(mode: ThemeMode | null | undefined) {
  if (!mode) {
    return;
  }
  window.localStorage.setItem(USER_THEME_STORAGE_KEY, mode);
}

export function resolveThemeMode(mode: ThemeMode | null | undefined): ResolvedTheme {
  if (mode === "light") {
    return "light";
  }
  if (mode === "enterprise" || mode === "sixmanager") {
    return "enterprise";
  }
  if (mode === "system") {
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  return "dark";
}

export function applyThemeToDocument(mode: ThemeMode | null | undefined) {
  const resolvedTheme = resolveThemeMode(mode);
  document.documentElement.dataset.theme = resolvedTheme;
  if (mode === "sixmanager") {
    document.documentElement.dataset.brandTheme = "sixmanager";
  } else {
    delete document.documentElement.dataset.brandTheme;
  }
  document.documentElement.style.colorScheme = resolvedTheme === "light" ? "light" : "dark";
  return resolvedTheme;
}

export function resolveEffectiveThemeMode(
  instanceTheme: ThemeMode | null | undefined,
  userPreference: UserThemePreference | null | undefined,
): ThemeMode {
  if (userPreference && userPreference !== "default") {
    return userPreference;
  }
  return instanceTheme ?? "dark";
}

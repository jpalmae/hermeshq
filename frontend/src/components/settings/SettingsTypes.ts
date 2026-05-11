export type SettingsTab =
  | "general"
  | "runtime"
  | "providers"
  | "integrations"
  | "factory"
  | "externalAccess"
  | "hermesVersions"
  | "secrets"
  | "templates";

export const SETTINGS_TAB_STORAGE_KEY = "hermeshq.settings.activeTab";

export const SETTINGS_TAB_IDS: SettingsTab[] = [
  "general",
  "runtime",
  "providers",
  "integrations",
  "factory",
  "externalAccess",
  "hermesVersions",
  "secrets",
  "templates",
];

import { createContext, useContext } from "react";
import type { ReactNode } from "react";

import type { AppSettings, User } from "../types/api";

import { en, es } from "./i18n/locales";

export type Locale = "en" | "es";
export type UserLocalePreference = User["locale_preference"];

type Dictionary = Record<string, string>;

const dictionaries: Record<Locale, Dictionary> = { en, es };

type I18nContextValue = {
  locale: Locale;
  t: (key: string, vars?: Record<string, string | number>) => string;
  formatDateTime: (value: string | number | Date) => string;
};

const I18nContext = createContext<I18nContextValue>({
  locale: "en",
  t: (key) => key,
  formatDateTime: (value) => new Date(value).toLocaleString("en-US"),
});

function interpolate(template: string, vars?: Record<string, string | number>) {
  if (!vars) {
    return template;
  }
  return template.replace(/\{(\w+)\}/g, (_, key: string) => String(vars[key] ?? `{${key}}`));
}

export function resolveEffectiveLocale(
  instanceLocale: AppSettings["default_locale"] | null | undefined,
  userPreference: UserLocalePreference | null | undefined,
): Locale {
  if (userPreference && userPreference !== "default") {
    return userPreference;
  }
  return instanceLocale ?? "en";
}

export function I18nProvider({ locale, children }: { locale: Locale; children: ReactNode }) {
  const dictionary = dictionaries[locale] ?? dictionaries.en;
  const value: I18nContextValue = {
    locale,
    t: (key, vars) => interpolate(dictionary[key] ?? dictionaries.en[key] ?? key, vars),
    formatDateTime: (input) => new Date(input).toLocaleString(locale === "es" ? "es-CL" : "en-US"),
  };
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  return useContext(I18nContext);
}

import { Navigate, Route, Routes } from "react-router-dom";
import { useEffect } from "react";

import { useMe } from "./api/auth";
import { usePublicBranding, resolveAssetUrl } from "./api/settings";
import { AppShell } from "./components/layout/AppShell";
import { I18nProvider, resolveEffectiveLocale } from "./lib/i18n";
import { applyThemeToDocument, resolveEffectiveThemeMode } from "./lib/theme";
import { useSessionStore } from "./stores/sessionStore";
import { AgentDetailPage } from "./pages/AgentDetailPage";
import { AgentsPage } from "./pages/AgentsPage";
import { CommsPage } from "./pages/CommsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { LoginPage } from "./pages/LoginPage";
import { ManualPage } from "./pages/ManualPage";
import { MyAccountPage } from "./pages/MyAccountPage";
import { NodesPage } from "./pages/NodesPage";
import { SettingsPage } from "./pages/SettingsPage";
import { ScheduledTasksPage } from "./pages/ScheduledTasksPage";
import { TasksPage } from "./pages/TasksPage";
import { UsersPage } from "./pages/UsersPage";

export default function App() {
  const token = useSessionStore((state) => state.token);
  const setUser = useSessionStore((state) => state.setUser);
  const currentUser = useSessionStore((state) => state.user);
  const { data: branding } = usePublicBranding();
  const { data: me } = useMe(Boolean(token));
  const effectiveThemeMode = token
    ? resolveEffectiveThemeMode(branding?.theme_mode, currentUser?.theme_preference)
    : (branding?.theme_mode ?? "dark");
  const effectiveLocale = token
    ? resolveEffectiveLocale(branding?.default_locale, currentUser?.locale_preference)
    : (branding?.default_locale ?? "en");

  useEffect(() => {
    if (me) {
      setUser(me);
    }
  }, [me, setUser]);

  useEffect(() => {
    document.title = branding?.app_name || "HermesHQ";
    const mediaQuery = window.matchMedia("(prefers-color-scheme: light)");
    const syncTheme = () => {
      applyThemeToDocument(effectiveThemeMode);
    };
    syncTheme();
    document.documentElement.lang = effectiveLocale;
    mediaQuery.addEventListener("change", syncTheme);
    const href = resolveAssetUrl(branding?.favicon_url);
    const existing = document.querySelector<HTMLLinkElement>("link[rel='icon']");
    if (href) {
      const link = existing ?? document.createElement("link");
      link.rel = "icon";
      link.href = href;
      document.head.appendChild(link);
    } else if (existing) {
      existing.remove();
    }
    return () => {
      mediaQuery.removeEventListener("change", syncTheme);
    };
  }, [branding?.app_name, branding?.favicon_url, effectiveLocale, effectiveThemeMode]);

  if (!token) {
    return (
      <I18nProvider locale={effectiveLocale}>
        <LoginPage />
      </I18nProvider>
    );
  }

  return (
    <I18nProvider locale={effectiveLocale}>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/agents" element={<AgentsPage />} />
          <Route path="/agents/:agentId" element={<AgentDetailPage />} />
          <Route path="/tasks" element={<TasksPage />} />
          <Route path="/schedules" element={<ScheduledTasksPage />} />
          <Route path="/account" element={<MyAccountPage />} />
          <Route path="/manual" element={<ManualPage />} />
          <Route path="/users" element={<UsersPage />} />
          <Route path="/nodes" element={<NodesPage />} />
          <Route path="/comms" element={<CommsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </I18nProvider>
  );
}

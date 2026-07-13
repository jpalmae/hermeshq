import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useEffect } from "react";

import { useMe } from "./api/auth";
import { usePublicBranding, resolveAssetUrl } from "./api/settings";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { AppShell } from "./components/layout/AppShell";
import { I18nProvider, resolveEffectiveLocale } from "./lib/i18n";
import {
  applyThemeToDocument,
  cachePublicThemeMode,
  cacheUserThemeMode,
  getStoredPublicThemeMode,
  getStoredUserThemeMode,
  resolveEffectiveThemeMode,
} from "./lib/theme";
import { useSessionStore } from "./stores/sessionStore";
import { AgentDetailPage } from "./pages/AgentDetailPage";
import { AgentsPage } from "./pages/AgentsPage";
import { BuilderPage } from "./pages/BuilderPage";
import { CommsPage } from "./pages/CommsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { LoginPage } from "./pages/LoginPage";
import { ForgotPasswordPage } from "./pages/ForgotPasswordPage";
import { MfaVerifyPage } from "./pages/MfaVerifyPage";
import { ResetPasswordPage } from "./pages/ResetPasswordPage";
import { ManualPage } from "./pages/ManualPage";
import { MyAccountPage } from "./pages/MyAccountPage";
import { NodesPage } from "./pages/NodesPage";
import { AuditPage } from "./pages/AuditPage";
import { SettingsPage } from "./pages/SettingsPage";
import { ScheduledTasksPage } from "./pages/ScheduledTasksPage";
import { TasksPage } from "./pages/TasksPage";
import { UsersPage } from "./pages/UsersPage";

export default function App() {
  const location = useLocation();
  const token = useSessionStore((state) => state.token);
  const setSession = useSessionStore((state) => state.setSession);
  const setUser = useSessionStore((state) => state.setUser);
  const currentUser = useSessionStore((state) => state.user);
  const { data: branding } = usePublicBranding();
  const { data: me } = useMe(Boolean(token));
  const publicThemeMode = branding?.theme_mode ?? getStoredPublicThemeMode() ?? "dark";
  const storedUserThemeMode = getStoredUserThemeMode();
  const effectiveThemeMode = token
    ? (currentUser
      ? resolveEffectiveThemeMode(branding?.theme_mode, currentUser.theme_preference)
      : (storedUserThemeMode ?? publicThemeMode))
    : (storedUserThemeMode ?? publicThemeMode);
  const effectiveLocale = token
    ? resolveEffectiveLocale(branding?.default_locale, currentUser?.locale_preference)
    : (branding?.default_locale ?? "en");

  // Detect OIDC token in URL (e.g. /?token=...) before App redirects to /login
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const urlToken = params.get("token");
    if (urlToken && !token) {
      setSession(urlToken, null);
      window.history.replaceState({}, "", location.pathname);
    }
  }, [location.search, token, setSession]);

  useEffect(() => {
    if (me) {
      setUser(me);
    }
  }, [me, setUser]);

  useEffect(() => {
    cachePublicThemeMode(branding?.theme_mode);
  }, [branding?.theme_mode]);

  useEffect(() => {
    if (token && currentUser) {
      cacheUserThemeMode(resolveEffectiveThemeMode(branding?.theme_mode, currentUser.theme_preference));
    }
  }, [branding?.theme_mode, currentUser, token]);

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
        <ErrorBoundary resetKey={location.pathname}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/forgot-password" element={<ForgotPasswordPage />} />
            <Route path="/reset-password" element={<ResetPasswordPage />} />
            <Route path="/mfa-verify" element={<MfaVerifyPage />} />
            <Route path="*" element={<Navigate to="/login" state={{ from: location.pathname + location.search }} replace />} />
          </Routes>
        </ErrorBoundary>
      </I18nProvider>
    );
  }

  return (
    <I18nProvider locale={effectiveLocale}>
      <ErrorBoundary resetKey={location.pathname}>
        <Routes>
          <Route element={<AppShell />}>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/agents" element={<AgentsPage />} />
            <Route path="/agents/:agentId" element={<AgentDetailPage />} />
            <Route path="/builder" element={<BuilderPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/schedules" element={<ScheduledTasksPage />} />
            <Route path="/account" element={<MyAccountPage />} />
            <Route path="/manual" element={<ManualPage />} />
            <Route path="/users" element={<UsersPage />} />
            <Route path="/nodes" element={<NodesPage />} />
            <Route path="/comms" element={<CommsPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/audit" element={<AuditPage />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </ErrorBoundary>
    </I18nProvider>
  );
}

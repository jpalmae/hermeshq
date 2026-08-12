import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { lazy, Suspense, useEffect, useRef, useState } from "react";

import { useMe, exchangeOidcCookie } from "./api/auth";
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
import { LoginPage } from "./pages/LoginPage";
import { ForgotPasswordPage } from "./pages/ForgotPasswordPage";
import { MfaVerifyPage } from "./pages/MfaVerifyPage";
import { ResetPasswordPage } from "./pages/ResetPasswordPage";

const AgentDetailPage = lazy(() => import("./pages/AgentDetailPage").then(m => ({ default: m.AgentDetailPage })));
const AgentsPage = lazy(() => import("./pages/AgentsPage").then(m => ({ default: m.AgentsPage })));
const BuilderPage = lazy(() => import("./pages/BuilderPage").then(m => ({ default: m.BuilderPage })));
const CommsPage = lazy(() => import("./pages/CommsPage").then(m => ({ default: m.CommsPage })));
const DashboardPage = lazy(() => import("./pages/DashboardPage").then(m => ({ default: m.DashboardPage })));
const ManualPage = lazy(() => import("./pages/ManualPage").then(m => ({ default: m.ManualPage })));
const MyAccountPage = lazy(() => import("./pages/MyAccountPage").then(m => ({ default: m.MyAccountPage })));
const NodesPage = lazy(() => import("./pages/NodesPage").then(m => ({ default: m.NodesPage })));
const AuditPage = lazy(() => import("./pages/AuditPage").then(m => ({ default: m.AuditPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then(m => ({ default: m.SettingsPage })));
const ScheduledTasksPage = lazy(() => import("./pages/ScheduledTasksPage").then(m => ({ default: m.ScheduledTasksPage })));
const TasksPage = lazy(() => import("./pages/TasksPage").then(m => ({ default: m.TasksPage })));
const UsersPage = lazy(() => import("./pages/UsersPage").then(m => ({ default: m.UsersPage })));
const V2Shell = lazy(() => import("./v2/components/V2Shell").then(m => ({ default: m.V2Shell })));
const V2Dashboard = lazy(() => import("./v2/pages/V2Dashboard").then(m => ({ default: m.V2Dashboard })));
const V2AgentsPage = lazy(() => import("./v2/pages/V2AgentsPage").then(m => ({ default: m.V2AgentsPage })));
const V2AgentDetailPage = lazy(() => import("./v2/pages/V2AgentDetailPage").then(m => ({ default: m.V2AgentDetailPage })));
const V2TasksPage = lazy(() => import("./v2/pages/V2TasksPage").then(m => ({ default: m.V2TasksPage })));
const V2SchedulesPage = lazy(() => import("./v2/pages/V2SchedulesPage").then(m => ({ default: m.V2SchedulesPage })));
const V2CommsPage = lazy(() => import("./v2/pages/V2CommsPage").then(m => ({ default: m.V2CommsPage })));
const V2SettingsPage = lazy(() => import("./v2/pages/V2SettingsPage").then(m => ({ default: m.V2SettingsPage })));
const V2UsersPage = lazy(() => import("./v2/pages/V2UsersPage").then(m => ({ default: m.V2UsersPage })));
const V2MyAccountPage = lazy(() => import("./v2/pages/V2MyAccountPage").then(m => ({ default: m.V2MyAccountPage })));
const V2AuditPage = lazy(() => import("./v2/pages/V2AuditPage").then(m => ({ default: m.V2AuditPage })));
const V2NodesPage = lazy(() => import("./v2/pages/V2NodesPage").then(m => ({ default: m.V2NodesPage })));
const V2BuilderPage = lazy(() => import("./v2/pages/V2BuilderPage").then(m => ({ default: m.V2BuilderPage })));
const V3Shell = lazy(() => import("./v3/components/V3Shell").then(m => ({ default: m.V3Shell })));
const V3Dashboard = lazy(() => import("./v3/pages/V3Dashboard").then(m => ({ default: m.V3Dashboard })));
const V3AgentsPage = lazy(() => import("./v3/pages/V3AgentsPage").then(m => ({ default: m.V3AgentsPage })));
const V3AgentDetailPage = lazy(() => import("./v3/pages/V3AgentDetailPage").then(m => ({ default: m.V3AgentDetailPage })));
const V3TasksPage = lazy(() => import("./v3/pages/V3TasksPage").then(m => ({ default: m.V3TasksPage })));

function PageFallback() {
  return (
    <div className="flex min-h-[50vh] items-center justify-center">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-[var(--border)] border-t-[var(--accent)]" />
    </div>
  );
}

export default function App() {
  const location = useLocation();
  const token = useSessionStore((state) => state.token);
  const setSession = useSessionStore((state) => state.setSession);
  const setUser = useSessionStore((state) => state.setUser);
  const currentUser = useSessionStore((state) => state.user);
  const [sessionReady, setSessionReady] = useState(false);
  const bootstrapStarted = useRef(false);
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

  useEffect(() => {
    if (bootstrapStarted.current) return;
    bootstrapStarted.current = true;
    const params = new URLSearchParams(location.search);
    if (params.get("oidc") === "complete") {
      window.history.replaceState({}, "", location.pathname);
    }
    void exchangeOidcCookie()
      .then((newToken) => {
        if (newToken) setSession(newToken, null);
      })
      .finally(() => setSessionReady(true));
  }, [location.pathname, location.search, setSession]);

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

  if (!sessionReady) {
    return <PageFallback />;
  }

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
            <Route path="/" element={<Suspense fallback={<PageFallback />}><DashboardPage /></Suspense>} />
            <Route path="/agents" element={<Suspense fallback={<PageFallback />}><AgentsPage /></Suspense>} />
            <Route path="/agents/:agentId" element={<Suspense fallback={<PageFallback />}><AgentDetailPage /></Suspense>} />
            <Route path="/builder" element={<Suspense fallback={<PageFallback />}><BuilderPage /></Suspense>} />
            <Route path="/tasks" element={<Suspense fallback={<PageFallback />}><TasksPage /></Suspense>} />
            <Route path="/schedules" element={<Suspense fallback={<PageFallback />}><ScheduledTasksPage /></Suspense>} />
            <Route path="/account" element={<Suspense fallback={<PageFallback />}><MyAccountPage /></Suspense>} />
            <Route path="/manual" element={<Suspense fallback={<PageFallback />}><ManualPage /></Suspense>} />
            <Route path="/users" element={<Suspense fallback={<PageFallback />}><UsersPage /></Suspense>} />
            <Route path="/nodes" element={<Suspense fallback={<PageFallback />}><NodesPage /></Suspense>} />
            <Route path="/comms" element={<Suspense fallback={<PageFallback />}><CommsPage /></Suspense>} />
            <Route path="/settings" element={<Suspense fallback={<PageFallback />}><SettingsPage /></Suspense>} />
            <Route path="/audit" element={<Suspense fallback={<PageFallback />}><AuditPage /></Suspense>} />
          </Route>
          <Route path="/v2" element={<Suspense fallback={<PageFallback />}><V2Shell /></Suspense>}>
            <Route index element={<Suspense fallback={<PageFallback />}><V2Dashboard /></Suspense>} />
            <Route path="agents" element={<Suspense fallback={<PageFallback />}><V2AgentsPage /></Suspense>} />
            <Route path="agents/:agentId" element={<Suspense fallback={<PageFallback />}><V2AgentDetailPage /></Suspense>} />
            <Route path="builder" element={<Suspense fallback={<PageFallback />}><V2BuilderPage /></Suspense>} />
            <Route path="tasks" element={<Suspense fallback={<PageFallback />}><V2TasksPage /></Suspense>} />
            <Route path="schedules" element={<Suspense fallback={<PageFallback />}><V2SchedulesPage /></Suspense>} />
            <Route path="comms" element={<Suspense fallback={<PageFallback />}><V2CommsPage /></Suspense>} />
            <Route path="audit" element={<Suspense fallback={<PageFallback />}><V2AuditPage /></Suspense>} />
            <Route path="nodes" element={<Suspense fallback={<PageFallback />}><V2NodesPage /></Suspense>} />
            <Route path="users" element={<Suspense fallback={<PageFallback />}><V2UsersPage /></Suspense>} />
            <Route path="settings" element={<Suspense fallback={<PageFallback />}><V2SettingsPage /></Suspense>} />
            <Route path="account" element={<Suspense fallback={<PageFallback />}><V2MyAccountPage /></Suspense>} />
          </Route>
          <Route path="/v3" element={<Suspense fallback={<PageFallback />}><V3Shell /></Suspense>}>
            <Route index element={<Suspense fallback={<PageFallback />}><V3Dashboard /></Suspense>} />
            <Route path="agents" element={<Suspense fallback={<PageFallback />}><V3AgentsPage /></Suspense>} />
            <Route path="agents/:agentId" element={<Suspense fallback={<PageFallback />}><V3AgentDetailPage /></Suspense>} />
            <Route path="tasks" element={<Suspense fallback={<PageFallback />}><V3TasksPage /></Suspense>} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </ErrorBoundary>
    </I18nProvider>
  );
}

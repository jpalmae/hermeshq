import { Navigate, Route, Routes } from "react-router-dom";
import { useEffect } from "react";

import { usePublicBranding, resolveAssetUrl } from "./api/settings";
import { AppShell } from "./components/layout/AppShell";
import { applyThemeToDocument } from "./lib/theme";
import { useSessionStore } from "./stores/sessionStore";
import { AgentDetailPage } from "./pages/AgentDetailPage";
import { AgentsPage } from "./pages/AgentsPage";
import { CommsPage } from "./pages/CommsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { LoginPage } from "./pages/LoginPage";
import { NodesPage } from "./pages/NodesPage";
import { SettingsPage } from "./pages/SettingsPage";
import { ScheduledTasksPage } from "./pages/ScheduledTasksPage";
import { TasksPage } from "./pages/TasksPage";

export default function App() {
  const token = useSessionStore((state) => state.token);
  const { data: branding } = usePublicBranding();

  useEffect(() => {
    document.title = branding?.app_name || "HermesHQ";
    const mediaQuery = window.matchMedia("(prefers-color-scheme: light)");
    const syncTheme = () => {
      applyThemeToDocument(branding?.theme_mode);
    };
    syncTheme();
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
  }, [branding?.app_name, branding?.favicon_url, branding?.theme_mode]);

  if (!token) {
    return <LoginPage />;
  }

  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/agents" element={<AgentsPage />} />
        <Route path="/agents/:agentId" element={<AgentDetailPage />} />
        <Route path="/tasks" element={<TasksPage />} />
        <Route path="/schedules" element={<ScheduledTasksPage />} />
        <Route path="/nodes" element={<NodesPage />} />
        <Route path="/comms" element={<CommsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

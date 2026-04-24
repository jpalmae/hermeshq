import { useEffect, type CSSProperties } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";

import { buildOidcLogoutUrl, useMe, useUpdateMyPreferences } from "../../api/auth";
import { UserAvatar } from "../UserAvatar";
import { resolveAssetUrl, usePublicBranding } from "../../api/settings";
import { useWebSocket } from "../../hooks/useWebSocket";
import { useI18n } from "../../lib/i18n";
import { useSessionStore } from "../../stores/sessionStore";
import { useUIStore } from "../../stores/uiStore";

export function AppShell() {
  const token = useSessionStore((state) => state.token);
  const logout = useSessionStore((state) => state.logout);
  const setUser = useSessionStore((state) => state.setUser);
  const sidebarCollapsed = useUIStore((state) => state.sidebarCollapsed);
  const toggleSidebar = useUIStore((state) => state.toggleSidebar);
  const mobileNavOpen = useUIStore((state) => state.mobileNavOpen);
  const setMobileNavOpen = useUIStore((state) => state.setMobileNavOpen);
  const { data: user } = useMe(Boolean(token));
  const { data: branding } = usePublicBranding();
  const updatePreferences = useUpdateMyPreferences();
  const { t } = useI18n();
  const logoUrl = resolveAssetUrl(branding?.logo_url);
  const appName = branding?.app_name || "HermesHQ";
  const appShortName = branding?.app_short_name || appName;
  const appVersion = branding?.app_version || "";

  useWebSocket();

  function handleLogout() {
    const authSource = user?.auth_source;
    logout();
    if (authSource === "oidc") {
      window.location.assign(buildOidcLogoutUrl());
      return;
    }
    window.location.assign("/");
  }

  useEffect(() => {
    if (user) {
      setUser(user);
    }
  }, [setUser, user]);

  const navItems = user?.role === "admin"
    ? [
        { to: "/", label: "Overview" },
        { to: "/agents", label: "Agents" },
        { to: "/tasks", label: "Tasks" },
        { to: "/schedules", label: "Schedules" },
        { to: "/users", label: "Users" },
        { to: "/nodes", label: "Nodes" },
        { to: "/comms", label: "Comms" },
        { to: "/settings", label: "Settings" },
      ]
    : [
        { to: "/", label: "Overview" },
        { to: "/agents", label: "Agents" },
        { to: "/tasks", label: "Tasks" },
        { to: "/schedules", label: "Schedules" },
        { to: "/comms", label: "Comms" },
      ];

  const localizedNavItems = navItems.map((item) => ({
    ...item,
    label: t(`nav.${item.label.charAt(0).toLowerCase()}${item.label.slice(1)}`),
  }));

  const navContent = (
    <>
      <div className="app-shell-nav-content space-y-10">
        <Link to="/" className="block">
          <p className="panel-label">{sidebarCollapsed ? appShortName : t("shell.nodeControl", { appName })}</p>
          <div className="app-shell-brand mt-4">
            {logoUrl ? (
              <img
                src={logoUrl}
                alt={appName}
                className={`${sidebarCollapsed ? "mx-auto h-11 w-11" : "h-12 w-auto max-w-[11rem]"} object-contain`}
              />
            ) : (
              <h1 className="font-display text-[2.25rem] leading-none text-[var(--text-display)]">
                {sidebarCollapsed ? appShortName.slice(0, 2).toUpperCase() : appShortName}
              </h1>
            )}
            {!sidebarCollapsed ? (
              <p className="mt-3 max-w-[18ch] text-sm text-[var(--text-secondary)]">
                {t("shell.multiAgent")}
              </p>
            ) : null}
            {!sidebarCollapsed && appVersion ? (
              <p className="mt-3 text-xs uppercase tracking-[0.12em] text-[var(--text-disabled)]">
                v{appVersion}
              </p>
            ) : null}
          </div>
        </Link>
        <nav className="space-y-2">
          {localizedNavItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              onClick={() => setMobileNavOpen(false)}
              className={({ isActive }) =>
                `app-shell-nav-link flex items-center ${sidebarCollapsed ? "justify-center" : "justify-between"} border-b border-[var(--border)] py-3 text-sm uppercase tracking-[0.12em] ${
                  isActive ? "text-[var(--text-display)]" : "text-[var(--text-secondary)]"
                }`
              }
              title={item.label}
            >
              <span>{sidebarCollapsed ? item.label.slice(0, 2) : item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="space-y-4 border-t border-[var(--border)] pt-4">
          <p className="panel-label">{t("shell.operator")}</p>
          {!sidebarCollapsed ? (
            <>
              <div className="app-shell-operator mt-2 flex items-center gap-3">
                {user ? <UserAvatar user={user} sizeClass="h-10 w-10" className="shrink-0" /> : null}
                <p className="text-sm text-[var(--text-display)]">{user?.display_name ?? "..."}</p>
              </div>
              <p className="mt-1 text-xs uppercase tracking-[0.12em] text-[var(--text-disabled)]">
                {(user?.role ?? t("shell.offline"))} / {user?.username ?? t("shell.offline")}
              </p>
              <NavLink
                to="/account"
                onClick={() => setMobileNavOpen(false)}
                className={({ isActive }) =>
                  `app-shell-nav-link flex items-center justify-between border-b border-[var(--border)] py-3 text-sm uppercase tracking-[0.12em] ${
                    isActive ? "text-[var(--text-display)]" : "text-[var(--text-secondary)]"
                  }`
                }
              >
                <span>{t("nav.myAccount")}</span>
              </NavLink>
              <NavLink
                to="/manual"
                onClick={() => setMobileNavOpen(false)}
                className={({ isActive }) =>
                  `app-shell-nav-link flex items-center justify-between border-b border-[var(--border)] py-3 text-sm uppercase tracking-[0.12em] ${
                    isActive ? "text-[var(--text-display)]" : "text-[var(--text-secondary)]"
                  }`
                }
              >
                <span>{t("nav.manual")}</span>
              </NavLink>
              <label className="panel-field mt-4">
                <span className="panel-label">{t("shell.myTheme")}</span>
                <select
                  value={user?.theme_preference ?? "default"}
                  onChange={(event) => {
                    void updatePreferences.mutateAsync({
                      theme_preference: event.target.value as "default" | "dark" | "light" | "system" | "enterprise" | "sixmanager" | "sixmanager-light",
                    }).then((updatedUser) => {
                      setUser(updatedUser);
                    });
                  }}
                >
                  <option value="default">{t("common.useInstanceDefault")}</option>
                  <option value="dark">{t("common.dark")}</option>
                  <option value="light">{t("common.light")}</option>
                  <option value="system">{t("common.system")}</option>
                  <option value="enterprise">{t("common.enterprise")}</option>
                  <option value="sixmanager">{t("common.sixmanager")}</option>
                  <option value="sixmanager-light">{t("common.sixmanagerLight")}</option>
                </select>
              </label>
              <label className="panel-field">
                <span className="panel-label">{t("shell.myLanguage")}</span>
                <select
                  value={user?.locale_preference ?? "default"}
                  onChange={(event) => {
                    void updatePreferences.mutateAsync({
                      locale_preference: event.target.value as "default" | "en" | "es",
                    }).then((updatedUser) => {
                      setUser(updatedUser);
                    });
                  }}
                >
                  <option value="default">{t("common.useInstanceDefault")}</option>
                  <option value="en">{t("common.english")}</option>
                  <option value="es">{t("common.spanish")}</option>
                </select>
              </label>
            </>
          ) : (
            <>
              <div className="mt-2 flex justify-center">
                {user ? <UserAvatar user={user} sizeClass="h-10 w-10" /> : <p className="text-center text-sm text-[var(--text-display)]">…</p>}
              </div>
              <NavLink
                to="/account"
                onClick={() => setMobileNavOpen(false)}
                className={({ isActive }) =>
                  `app-shell-nav-link flex items-center justify-center border-b border-[var(--border)] py-3 text-sm uppercase tracking-[0.12em] ${
                    isActive ? "text-[var(--text-display)]" : "text-[var(--text-secondary)]"
                  }`
                }
                title={t("nav.myAccount")}
              >
                <span>AC</span>
              </NavLink>
              <NavLink
                to="/manual"
                onClick={() => setMobileNavOpen(false)}
                className={({ isActive }) =>
                  `app-shell-nav-link flex items-center justify-center border-b border-[var(--border)] py-3 text-sm uppercase tracking-[0.12em] ${
                    isActive ? "text-[var(--text-display)]" : "text-[var(--text-secondary)]"
                  }`
                }
                title={t("nav.manual")}
              >
                <span>MA</span>
              </NavLink>
            </>
          )}
          <button type="button" className="panel-button-secondary w-full" onClick={handleLogout}>
            {sidebarCollapsed ? "Out" : t("nav.signOut")}
          </button>
        </div>
      </div>
    </>
  );

  return (
    <div className="app-shell-root min-h-screen bg-[var(--black)] text-[var(--text-primary)]">
      <div
        className="app-shell-grid mx-auto grid min-h-screen max-w-[1600px] grid-cols-1 gap-8 px-4 py-4 md:grid-cols-[var(--sidebar-width)_minmax(0,1fr)] md:px-8 md:py-8"
        style={
          {
            "--sidebar-width": sidebarCollapsed ? "92px" : "260px",
          } as CSSProperties
        }
      >
        <div className="hidden md:block">
          <aside
            className={`app-shell-sidebar panel-frame flex h-full flex-col justify-between p-6 transition-[width,padding] duration-200 md:min-h-[calc(100vh-4rem)] ${
              sidebarCollapsed ? "items-center px-4" : ""
            }`}
          >
            <div className="mb-6 flex w-full justify-end">
              <button type="button" className="app-shell-sidebar-toggle panel-button-secondary !min-h-0 px-4 py-2" onClick={toggleSidebar}>
                {sidebarCollapsed ? "»" : "«"}
              </button>
            </div>
            <div className={`flex flex-1 flex-col ${sidebarCollapsed ? "w-full items-center" : "w-full"}`}>
              {navContent}
            </div>
          </aside>
        </div>

        <main className="app-shell-main pb-8">
          <div className="mb-4 flex items-center justify-between gap-3 md:hidden">
            <button
              type="button"
              className="panel-button-secondary"
              onClick={() => setMobileNavOpen(true)}
            >
              {t("nav.menu")}
            </button>
            <button type="button" className="panel-button-secondary" onClick={toggleSidebar}>
              {sidebarCollapsed ? t("nav.expand") : t("nav.collapse")}
            </button>
          </div>
          <Outlet />
        </main>
      </div>

      {mobileNavOpen ? (
        <div className="fixed inset-0 z-50 bg-[var(--overlay)] md:hidden" onClick={() => setMobileNavOpen(false)}>
          <aside
            className="app-shell-mobile panel-frame absolute left-4 top-4 bottom-4 w-[min(82vw,320px)] p-6"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="mb-6 flex justify-end">
              <button type="button" className="panel-button-secondary !min-h-0 px-4 py-2" onClick={() => setMobileNavOpen(false)}>
                {t("nav.close")}
              </button>
            </div>
            <div className="flex h-[calc(100%-4rem)] flex-col">
              {navContent}
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}

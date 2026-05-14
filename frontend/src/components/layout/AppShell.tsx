import { useEffect, type CSSProperties } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";

import { buildOidcLogoutUrl, useMe, useUpdateMyPreferences } from "../../api/auth";
import { UserAvatar } from "../UserAvatar";
import { resolveAssetUrl, usePublicBranding } from "../../api/settings";
import { useWebSocket } from "../../hooks/useWebSocket";
import { useI18n } from "../../lib/i18n";
import { useSessionStore } from "../../stores/sessionStore";
import { useUIStore } from "../../stores/uiStore";

import resumenIcon from "../../assets/icon/resumen.png";
import agenteIcon from "../../assets/icon/agente.png";
import tareasIcon from "../../assets/icon/tareas.png";
import programacionesIcon from "../../assets/icon/programaciones.png";
import usuariosIcon from "../../assets/icon/usuarios.png";
import nodosIcon from "../../assets/icon/nodos.png";
import commsIcon from "../../assets/icon/comms.png";
import configuracionIcon from "../../assets/icon/configuracion.png";
import manualIcon from "../../assets/icon/manual.png";
import salirIcon from "../../assets/icon/salir.png";
import logoutIcon from "../../assets/icon/logout.png";
import menuIcon from "../../assets/icon/menu.png";

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

  // Sync useMe data to session store (required for cookie-based auth)
  useEffect(() => {
    if (user) {
      setUser(user);
    }
  }, [setUser, user]);

  function handleLogout() {
    const authSource = user?.auth_source;
    logout();
    if (authSource === "oidc") {
      window.location.assign(buildOidcLogoutUrl());
      return;
    }
    window.location.assign("/");
  }

  const navItems = user?.role === "admin"
    ? [
        { to: "/", label: "Overview", icon: resumenIcon },
        { to: "/agents", label: "Agents", icon: agenteIcon },
        { to: "/tasks", label: "Tasks", icon: tareasIcon },
        { to: "/schedules", label: "Schedules", icon: programacionesIcon },
        { to: "/users", label: "Users", icon: usuariosIcon },
        { to: "/nodes", label: "Nodes", icon: nodosIcon },
        { to: "/comms", label: "Comms", icon: commsIcon },
        { to: "/settings", label: "Settings", icon: configuracionIcon },
      ]
    : [
        { to: "/", label: "Overview", icon: resumenIcon },
        { to: "/agents", label: "Agents", icon: agenteIcon },
        { to: "/tasks", label: "Tasks", icon: tareasIcon },
        { to: "/schedules", label: "Schedules", icon: programacionesIcon },
        { to: "/comms", label: "Comms", icon: commsIcon },
      ];

  const localizedNavItems = navItems.map((item) => ({
    ...item,
    label: t(`nav.${item.label.charAt(0).toLowerCase()}${item.label.slice(1)}`),
  }));

  const renderNav = (collapsed: boolean) => (
    <div className={`app-shell-nav-content flex flex-col ${collapsed ? "gap-6" : "gap-4"}`}>

      {/* Brand */}
      <Link to="/" className="block">
        {!collapsed && (
          <p className="panel-label">{t("shell.nodeControl", { appName })}</p>
        )}
        <div className={`app-shell-brand ${collapsed ? "mt-0" : "mt-3"}`}>
          {logoUrl ? (
            <img
              src={logoUrl}
              alt={appName}
              className={`${collapsed ? "mx-auto h-10 w-10" : "h-10 w-auto max-w-[11rem]"} object-contain`}
            />
          ) : !collapsed ? (
            <h1 className="font-display text-[2rem] leading-none text-[var(--text-display)]">
              {appShortName}
            </h1>
          ) : null}
          {!collapsed && (
            <p className="mt-2 max-w-[18ch] text-xs text-[var(--text-secondary)]">
              {t("shell.multiAgent")}
            </p>
          )}
          {!collapsed && appVersion && (
            <p className="mt-1 text-xs uppercase tracking-[0.12em] text-[var(--text-disabled)]">
              v{appVersion}
            </p>
          )}
        </div>
      </Link>

      {/* Nav items */}
      <nav className={`flex flex-col ${collapsed ? "gap-3" : ""}`}>
        {localizedNavItems.map((item, index) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === "/"}
            onClick={() => setMobileNavOpen(false)}
            className={({ isActive }) =>
              `app-shell-nav-link flex items-center ${collapsed ? "justify-center" : "justify-between"} py-2 text-sm uppercase tracking-[0.12em] ${
                index < localizedNavItems.length - 1 ? "border-b border-[var(--border)]" : ""
              } ${isActive ? "text-[var(--text-display)]" : "text-[var(--text-secondary)]"}`
            }
            title={item.label}
          >
            <span>
              {collapsed ? (
                <img src={item.icon} alt={item.label} className="app-shell-nav-icon h-5 w-5" />
              ) : (
                item.label
              )}
            </span>
          </NavLink>
        ))}
      </nav>

      {/* Operator section */}
      <div className={`flex flex-col ${collapsed ? "gap-2" : "gap-3"} border-t border-[var(--border)] pt-4`}>
        <p className="panel-label">{t("shell.operator")}</p>

        {!collapsed ? (
          <>
            <NavLink
              to="/account"
              onClick={() => setMobileNavOpen(false)}
              className="app-shell-operator flex items-center gap-3 transition-opacity hover:opacity-80"
            >
              {user ? <UserAvatar user={user} sizeClass="h-9 w-9" className="shrink-0" /> : null}
              <div className="min-w-0">
                <p className="truncate text-sm text-[var(--text-display)]">{user?.display_name ?? "..."}</p>
                <p className="text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                  {user?.role ?? t("shell.offline")} / {user?.username ?? t("shell.offline")}
                </p>
              </div>
            </NavLink>

            <NavLink
              to="/manual"
              onClick={() => setMobileNavOpen(false)}
              className={({ isActive }) =>
                `app-shell-nav-link flex items-center justify-between border-y border-[var(--border)] py-2 text-sm uppercase tracking-[0.12em] ${
                  isActive ? "text-[var(--text-display)]" : "text-[var(--text-secondary)]"
                }`
              }
            >
              <span>{t("nav.manual")}</span>
            </NavLink>

            <label className="panel-field">
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

            <button
              type="button"
              className="panel-button-secondary w-full rounded-none flex items-center justify-center gap-3 !py-1 !px-3"
              onClick={handleLogout}
            >
              <img src={logoutIcon} alt="" className="app-shell-nav-icon h-4 w-4 shrink-0" />
              {t("nav.signOut")}
            </button>
          </>
        ) : (
          <div className="flex flex-col gap-6">
            <NavLink
              to="/account"
              onClick={() => setMobileNavOpen(false)}
              className="flex justify-center transition-opacity hover:opacity-80"
              title={t("nav.myAccount")}
            >
              {user
                ? <UserAvatar user={user} sizeClass="h-9 w-9" />
                : <p className="text-center text-sm text-[var(--text-display)]">…</p>
              }
            </NavLink>

            <NavLink
              to="/manual"
              onClick={() => setMobileNavOpen(false)}
              className={({ isActive }) =>
                `app-shell-nav-link flex items-center justify-center border-y border-[var(--border)] py-2 text-sm ${
                  isActive ? "text-[var(--text-display)]" : "text-[var(--text-secondary)]"
                }`
              }
              title={t("nav.manual")}
            >
              <img src={manualIcon} alt={t("nav.manual")} className="app-shell-nav-icon h-5 w-5" />
            </NavLink>

            <button
              type="button"
              className="app-shell-nav-link flex items-center justify-center py-2 w-full text-[var(--text-secondary)]"
              onClick={handleLogout}
              title={t("nav.signOut")}
            >
              <img src={salirIcon} alt={t("nav.signOut")} className="app-shell-nav-icon h-5 w-5" />
            </button>
          </div>
        )}
      </div>
    </div>
  );

  return (
    <div className="app-shell-root min-h-screen bg-[var(--black)] text-[var(--text-primary)]">
      <div
        className="app-shell-grid mx-auto grid min-h-screen max-w-[1600px] grid-cols-1 gap-8 px-4 py-4 md:grid-cols-[var(--sidebar-width)_minmax(0,1fr)] md:px-8 md:py-8"
        style={{ "--sidebar-width": sidebarCollapsed ? "92px" : "260px" } as CSSProperties}
      >
        <div className="hidden md:block">
          <aside
            className={`app-shell-sidebar flex flex-col p-5 transition-[width,padding] duration-200 sticky top-8 max-h-[calc(100vh-4rem)] overflow-y-auto ${
              sidebarCollapsed ? "items-center px-3" : ""
            }`}
          >
            <div className={`mb-4 flex w-full ${sidebarCollapsed ? "justify-center" : "justify-end"}`}>
              <button
                type="button"
                className="app-shell-sidebar-toggle panel-button-secondary !min-h-0 px-3 py-1.5"
                onClick={toggleSidebar}
              >
                {sidebarCollapsed ? "»" : "«"}
              </button>
            </div>
            <div className={`flex flex-1 flex-col ${sidebarCollapsed ? "w-full items-center" : "w-full"}`}>
              {renderNav(sidebarCollapsed)}
            </div>
          </aside>
        </div>

        <main className="app-shell-main pb-8">
          {/* Mobile top bar */}
          <div className="mb-4 flex items-center md:hidden">
            <button
              type="button"
              className="app-shell-mobile-menu-btn panel-button-secondary !min-h-0 !rounded-none !p-2 flex items-center justify-center"
              onClick={() => setMobileNavOpen(true)}
            >
              <img src={menuIcon} alt={t("nav.menu")} className="app-shell-nav-icon h-5 w-5" />
            </button>
          </div>
          <Outlet />
        </main>
      </div>

      {mobileNavOpen && (
        <div
          className="fixed inset-0 z-50 bg-[var(--overlay)] md:hidden"
          onClick={() => setMobileNavOpen(false)}
        >
          <aside
            className="app-shell-mobile panel-frame absolute left-4 top-4 bottom-4 w-[min(82vw,320px)] p-5"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="mb-4 flex justify-end">
              <button
                type="button"
                className="app-shell-mobile-menu-btn panel-button-secondary !min-h-0 !rounded-none !p-2 flex items-center justify-center"
                onClick={() => setMobileNavOpen(false)}
              >
                «
              </button>
            </div>
            <div className="flex h-[calc(100%-3.5rem)] flex-col overflow-y-auto">
              {renderNav(false)}
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}

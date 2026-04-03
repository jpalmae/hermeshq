import { useEffect, type CSSProperties } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";

import { useMe } from "../../api/auth";
import { resolveAssetUrl, usePublicBranding } from "../../api/settings";
import { useWebSocket } from "../../hooks/useWebSocket";
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
  const logoUrl = resolveAssetUrl(branding?.logo_url);
  const appName = branding?.app_name || "HermesHQ";
  const appShortName = branding?.app_short_name || appName;

  useWebSocket();

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

  const navContent = (
    <>
      <div className="space-y-10">
        <Link to="/" className="block">
          <p className="panel-label">{sidebarCollapsed ? appShortName : `${appName} / Node Control`}</p>
          <div className="mt-4">
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
                Multi-agent control surface for continuous operations.
              </p>
            ) : null}
          </div>
        </Link>
        <nav className="space-y-2">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              onClick={() => setMobileNavOpen(false)}
              className={({ isActive }) =>
                `flex items-center ${sidebarCollapsed ? "justify-center" : "justify-between"} border-b border-[var(--border)] py-3 text-sm uppercase tracking-[0.12em] ${
                  isActive ? "text-[var(--text-display)]" : "text-[var(--text-secondary)]"
                }`
              }
              title={item.label}
            >
              <span>{sidebarCollapsed ? item.label.slice(0, 2) : item.label}</span>
            </NavLink>
          ))}
        </nav>
      </div>

      <div className="space-y-4">
        <div className="border-t border-[var(--border)] pt-4">
          <p className="panel-label">Operator</p>
          {!sidebarCollapsed ? (
            <>
              <p className="mt-2 text-sm text-[var(--text-display)]">{user?.display_name ?? "..."}</p>
              <p className="mt-1 text-xs uppercase tracking-[0.12em] text-[var(--text-disabled)]">
                {(user?.role ?? "offline")} / {user?.username ?? "offline"}
              </p>
            </>
          ) : (
            <p className="mt-2 text-center text-sm text-[var(--text-display)]">
              {user?.display_name?.slice(0, 1) ?? "…"}
            </p>
          )}
        </div>
        <button type="button" className="panel-button-secondary w-full" onClick={logout}>
          {sidebarCollapsed ? "Out" : "Sign out"}
        </button>
      </div>
    </>
  );

  return (
    <div className="min-h-screen bg-[var(--black)] text-[var(--text-primary)]">
      <div
        className="mx-auto grid min-h-screen max-w-[1600px] grid-cols-1 gap-8 px-4 py-4 md:grid-cols-[var(--sidebar-width)_minmax(0,1fr)] md:px-8 md:py-8"
        style={
          {
            "--sidebar-width": sidebarCollapsed ? "92px" : "260px",
          } as CSSProperties
        }
      >
        <div className="hidden md:block">
          <aside
            className={`panel-frame flex h-full flex-col justify-between p-6 transition-[width,padding] duration-200 md:min-h-[calc(100vh-4rem)] ${
              sidebarCollapsed ? "items-center px-4" : ""
            }`}
          >
            <div className="mb-6 flex w-full justify-end">
              <button type="button" className="panel-button-secondary !min-h-0 px-4 py-2" onClick={toggleSidebar}>
                {sidebarCollapsed ? "»" : "«"}
              </button>
            </div>
            <div className={`flex flex-1 flex-col justify-between ${sidebarCollapsed ? "w-full items-center" : "w-full"}`}>
              {navContent}
            </div>
          </aside>
        </div>

        <main className="pb-8">
          <div className="mb-4 flex items-center justify-between gap-3 md:hidden">
            <button
              type="button"
              className="panel-button-secondary"
              onClick={() => setMobileNavOpen(true)}
            >
              Menu
            </button>
            <button type="button" className="panel-button-secondary" onClick={toggleSidebar}>
              {sidebarCollapsed ? "Expand nav" : "Collapse nav"}
            </button>
          </div>
          <Outlet />
        </main>
      </div>

      {mobileNavOpen ? (
        <div className="fixed inset-0 z-50 bg-[var(--overlay)] md:hidden" onClick={() => setMobileNavOpen(false)}>
          <aside
            className="panel-frame absolute left-4 top-4 bottom-4 w-[min(82vw,320px)] p-6"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="mb-6 flex justify-end">
              <button type="button" className="panel-button-secondary !min-h-0 px-4 py-2" onClick={() => setMobileNavOpen(false)}>
                Close
              </button>
            </div>
            <div className="flex h-[calc(100%-4rem)] flex-col justify-between">
              {navContent}
            </div>
          </aside>
        </div>
      ) : null}
    </div>
  );
}

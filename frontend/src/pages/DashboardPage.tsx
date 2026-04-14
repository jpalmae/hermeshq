import { Link } from "react-router-dom";

import { useAgents } from "../api/agents";
import { AgentAvatar } from "../components/AgentAvatar";
import { useDashboardOverview } from "../api/dashboard";
import { AgentOrgChart } from "../components/AgentOrgChart";
import { useI18n } from "../lib/i18n";
import { UserAvatar } from "../components/UserAvatar";
import { useRealtimeStore } from "../stores/realtimeStore";
import { useSessionStore } from "../stores/sessionStore";

function statusTone(status: string) {
  if (status === "running") return "text-[var(--success)]";
  if (status === "queued" || status === "starting") return "text-[var(--warning)]";
  if (status === "error") return "text-[var(--accent)]";
  return "text-[var(--text-secondary)]";
}

function statusBadgeTone(status: string) {
  if (status === "running") return "border-[color-mix(in_srgb,var(--success)_45%,transparent)] bg-[color-mix(in_srgb,var(--success)_16%,transparent)] text-[var(--success)]";
  if (status === "queued" || status === "starting") return "border-[color-mix(in_srgb,var(--warning)_45%,transparent)] bg-[color-mix(in_srgb,var(--warning)_14%,transparent)] text-[var(--warning)]";
  if (status === "error" || status === "failed") return "border-[color-mix(in_srgb,var(--accent)_45%,transparent)] bg-[color-mix(in_srgb,var(--accent)_14%,transparent)] text-[var(--accent)]";
  return "border-[var(--border)] bg-[color-mix(in_srgb,var(--surface)_76%,transparent)] text-[var(--text-secondary)]";
}

export function DashboardPage() {
  const { data: overview } = useDashboardOverview();
  const { data: agents } = useAgents();
  const realtime = useRealtimeStore((state) => state.events);
  const currentUser = useSessionStore((state) => state.user);
  const { t, formatDateTime } = useI18n();
  const liveFeed = realtime.slice(0, 5);

  return (
    <div className="dashboard-page space-y-8">
      <section className="grid gap-6 xl:grid-cols-[0.7fr_1.3fr]">
        <div className="grid gap-6">
          <div className="dashboard-readout-card panel-frame p-4 md:p-5">
            <div>
              <p className="panel-label">{t("dashboard.primaryReadout")}</p>
              <div className="mt-2 flex items-end gap-3">
                <h2 className="font-display text-[clamp(2rem,4.8vw,3.2rem)] leading-[0.9] text-[var(--text-display)]">
                  {overview?.stats.active_agents ?? 0}
                </h2>
                <p className="max-w-[10ch] pb-0.5 text-[11px] leading-4 text-[var(--text-secondary)]">
                  {t("dashboard.activeAgentsLive")}
                </p>
              </div>
            </div>
            <div className="mt-4 border-t border-[var(--border)] pt-3">
              <div className="flex items-center gap-3">
                {currentUser ? <UserAvatar user={currentUser} sizeClass="h-11 w-11 md:h-12 md:w-12" className="shrink-0" /> : null}
                <div className="min-w-0">
                  <p className="panel-label">{t("dashboard.operator")}</p>
                  <p className="mt-1 truncate text-sm leading-4 text-[var(--text-display)]">
                    {currentUser?.display_name ?? t("common.unknown")}
                  </p>
                  <p className="mt-1 text-[10px] uppercase tracking-[0.1em] text-[var(--text-secondary)]">
                    {currentUser?.role ?? "offline"}
                  </p>
                </div>
              </div>
            </div>
            <div className="dashboard-metric-stack mt-4 grid gap-3 border-t border-[var(--border)] pt-3 md:grid-cols-3">
              <div className="dashboard-metric-chip">
                  <p className="panel-label">{t("dashboard.fleet")}</p>
                <p className="mt-1 text-base text-[var(--text-display)]">{overview?.stats.total_agents ?? 0}</p>
              </div>
              <div className="dashboard-metric-chip">
                  <p className="panel-label">{t("dashboard.queue")}</p>
                <p className="mt-1 text-base text-[var(--text-display)]">{overview?.stats.queued_tasks ?? 0}</p>
              </div>
              <div className="dashboard-metric-chip">
                  <p className="panel-label">{t("dashboard.tasks")}</p>
                <p className="mt-1 text-base text-[var(--text-display)]">{overview?.stats.total_tasks ?? 0}</p>
              </div>
            </div>
          </div>

          <div className="dashboard-feed-card panel-frame p-5">
            <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-3">
              <div>
                <p className="panel-label">{t("dashboard.liveFeed")}</p>
                <h3 className="mt-2 text-xl text-[var(--text-display)]">{t("dashboard.runtimeStream")}</h3>
              </div>
              <p className="panel-label">{t("dashboard.lines", { count: liveFeed.length })}</p>
            </div>
            <div className="mt-3 space-y-2">
              {liveFeed.map((event, index) => (
                <div key={`${event.type}-${index}`} className="dashboard-feed-row border-b border-[var(--border)] py-2 last:border-b-0">
                  <div className="flex items-center justify-between gap-3">
                    <p className="truncate text-xs uppercase tracking-[0.1em] text-[var(--text-secondary)]">
                      {event.type}
                    </p>
                    <span className={`dashboard-status-badge shrink-0 rounded-full border px-2.5 py-1 text-[10px] uppercase tracking-[0.1em] ${statusBadgeTone(event.status ?? "")}`}>
                      {event.status ?? "stream"}
                    </span>
                  </div>
                  <p className="mt-1 line-clamp-2 text-sm text-[var(--text-primary)]">
                    {event.message ?? event.response ?? t("dashboard.awaitingRuntimeOutput")}
                  </p>
                </div>
              ))}
              {!liveFeed.length ? <p className="panel-inline-status">{t("dashboard.eventStreamIdle")}</p> : null}
            </div>
          </div>
        </div>

        <section className="dashboard-map-card panel-frame p-6">
          <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
            <div>
              <p className="panel-label">{t("dashboard.agentMap")}</p>
              <h3 className="mt-2 text-2xl text-[var(--text-display)]">{t("dashboard.dependencyCanvas")}</h3>
            </div>
            <Link to="/agents" className="panel-button-secondary">
              {t("dashboard.openAgentStudio")}
            </Link>
          </div>
          <div className="mt-4">
            <AgentOrgChart agents={agents ?? []} />
          </div>
        </section>
      </section>

      <section className="grid gap-6 xl:grid-cols-[1.15fr_0.85fr]">
        <div className="dashboard-fleet-card panel-frame p-6">
          <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
            <div>
              <p className="panel-label">{t("dashboard.agents")}</p>
              <h3 className="mt-2 text-2xl text-[var(--text-display)]">{t("dashboard.currentFleet")}</h3>
            </div>
            <Link to="/agents" className="panel-button-secondary">
              {t("dashboard.openAgents")}
            </Link>
          </div>
          <div className="mt-2">
            {agents?.map((agent) => (
              <Link
                key={agent.id}
                to={`/agents/${agent.id}`}
                className="dashboard-agent-row grid gap-3 border-b border-[var(--border)] py-4 md:grid-cols-[1.4fr_1fr_1fr]"
              >
                <div className="flex items-start gap-4">
                  <AgentAvatar agent={agent} sizeClass="h-12 w-12" className="shrink-0" />
                  <div>
                    <p className="panel-label">{agent.slug}</p>
                    <p className="mt-2 text-lg text-[var(--text-display)]">{agent.friendly_name || agent.name}</p>
                    {agent.friendly_name && agent.friendly_name !== agent.name ? (
                      <p className="mt-2 text-sm text-[var(--text-secondary)]">{agent.name}</p>
                    ) : null}
                  </div>
                </div>
                <div>
                  <p className="panel-label">{t("dashboard.model")}</p>
                  <p className="mt-2 text-sm text-[var(--text-primary)]">{agent.model}</p>
                </div>
                <div className="text-left md:text-right">
                  <p className="panel-label">{t("dashboard.status")}</p>
                  <p className={`dashboard-agent-status mt-2 inline-flex rounded-full border px-2.5 py-1 text-sm uppercase tracking-[0.1em] ${statusBadgeTone(agent.status)}`}>
                    {agent.status}
                  </p>
                </div>
              </Link>
            ))}
          </div>
        </div>

        <div className="dashboard-activity-card panel-frame p-6">
          <p className="panel-label">{t("dashboard.recentActivity")}</p>
          <div className="mt-6 space-y-4">
            {overview?.activity.map((item) => (
              <div key={item.id} className="dashboard-activity-row border-b border-[var(--border)] pb-4">
                <p className="panel-label">{item.event_type}</p>
                <p className="mt-2 text-sm text-[var(--text-primary)]">{item.message ?? t("dashboard.noMessage")}</p>
                <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                  {formatDateTime(item.created_at)}
                </p>
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}

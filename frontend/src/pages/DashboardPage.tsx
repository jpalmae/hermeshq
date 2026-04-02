import { Link } from "react-router-dom";

import { useAgents } from "../api/agents";
import { useDashboardOverview } from "../api/dashboard";
import { AgentOrgChart } from "../components/AgentOrgChart";
import { useRealtimeStore } from "../stores/realtimeStore";

function statusTone(status: string) {
  if (status === "running") return "text-[var(--success)]";
  if (status === "queued" || status === "starting") return "text-[var(--warning)]";
  if (status === "error") return "text-[var(--accent)]";
  return "text-[var(--text-secondary)]";
}

export function DashboardPage() {
  const { data: overview } = useDashboardOverview();
  const { data: agents } = useAgents();
  const realtime = useRealtimeStore((state) => state.events);

  return (
    <div className="space-y-8">
      <section className="grid gap-6 xl:grid-cols-[1.35fr_0.65fr]">
        <div className="panel-frame flex min-h-[360px] flex-col justify-between p-8 md:p-10">
          <div className="flex items-start justify-between gap-6">
            <div>
              <p className="panel-label">Primary Readout</p>
              <h2 className="mt-4 font-display text-[clamp(4rem,12vw,8rem)] leading-[0.9] text-[var(--text-display)]">
                {overview?.stats.active_agents ?? 0}
              </h2>
              <p className="mt-3 text-lg text-[var(--text-primary)]">Active agents in circulation</p>
            </div>
            <div className="hidden h-28 w-28 rounded-full border border-[var(--border-visible)] md:block" />
          </div>
          <div className="grid gap-6 border-t border-[var(--border)] pt-6 md:grid-cols-3">
            <div>
              <p className="panel-label">Fleet</p>
              <p className="mt-2 text-2xl text-[var(--text-display)]">
                {overview?.stats.total_agents ?? 0}
              </p>
            </div>
            <div>
              <p className="panel-label">Queue</p>
              <p className="mt-2 text-2xl text-[var(--text-display)]">
                {overview?.stats.queued_tasks ?? 0}
              </p>
            </div>
            <div>
              <p className="panel-label">Tasks</p>
              <p className="mt-2 text-2xl text-[var(--text-display)]">
                {overview?.stats.total_tasks ?? 0}
              </p>
            </div>
          </div>
        </div>

        <div className="panel-frame p-6">
          <p className="panel-label">Live Feed</p>
          <div className="mt-6 space-y-4">
            {realtime.slice(0, 5).map((event, index) => (
              <div key={`${event.type}-${index}`} className="border-b border-[var(--border)] pb-4">
                <div className="flex items-center justify-between gap-3">
                  <p className="text-sm uppercase tracking-[0.1em] text-[var(--text-secondary)]">
                    {event.type}
                  </p>
                  <span className={`text-xs uppercase tracking-[0.1em] ${statusTone(event.status ?? "")}`}>
                    {event.status ?? "stream"}
                  </span>
                </div>
                <p className="mt-2 text-sm text-[var(--text-primary)]">
                  {event.message ?? event.response ?? "Awaiting runtime output"}
                </p>
              </div>
            ))}
            {!realtime.length ? <p className="panel-inline-status">[LOADING] event stream idle</p> : null}
          </div>
        </div>
      </section>

      <section className="panel-frame p-6">
        <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
          <div>
            <p className="panel-label">Agent map</p>
            <h3 className="mt-2 text-2xl text-[var(--text-display)]">Dependency canvas</h3>
          </div>
          <Link to="/agents" className="panel-button-secondary">
            Open agent studio
          </Link>
        </div>
        <div className="mt-6">
          <AgentOrgChart agents={agents ?? []} />
        </div>
      </section>

      <section className="grid gap-6 xl:grid-cols-[1.15fr_0.85fr]">
        <div className="panel-frame p-6">
          <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
            <div>
              <p className="panel-label">Agents</p>
              <h3 className="mt-2 text-2xl text-[var(--text-display)]">Current fleet</h3>
            </div>
            <Link to="/agents" className="panel-button-secondary">
              Open agents
            </Link>
          </div>
          <div className="mt-2">
            {agents?.map((agent) => (
              <Link
                key={agent.id}
                to={`/agents/${agent.id}`}
                className="grid gap-3 border-b border-[var(--border)] py-4 md:grid-cols-[1.4fr_1fr_1fr]"
              >
                <div>
                  <p className="panel-label">{agent.slug}</p>
                  <p className="mt-2 text-lg text-[var(--text-display)]">{agent.friendly_name || agent.name}</p>
                  {agent.friendly_name && agent.friendly_name !== agent.name ? (
                    <p className="mt-2 text-sm text-[var(--text-secondary)]">{agent.name}</p>
                  ) : null}
                </div>
                <div>
                  <p className="panel-label">Model</p>
                  <p className="mt-2 text-sm text-[var(--text-primary)]">{agent.model}</p>
                </div>
                <div className="text-left md:text-right">
                  <p className="panel-label">Status</p>
                  <p className={`mt-2 text-sm uppercase tracking-[0.1em] ${statusTone(agent.status)}`}>
                    {agent.status}
                  </p>
                </div>
              </Link>
            ))}
          </div>
        </div>

        <div className="panel-frame p-6">
          <p className="panel-label">Recent activity</p>
          <div className="mt-6 space-y-4">
            {overview?.activity.map((item) => (
              <div key={item.id} className="border-b border-[var(--border)] pb-4">
                <p className="panel-label">{item.event_type}</p>
                <p className="mt-2 text-sm text-[var(--text-primary)]">{item.message ?? "No message"}</p>
                <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                  {new Date(item.created_at).toLocaleString()}
                </p>
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}

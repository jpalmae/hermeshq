import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useAgents } from "../api/agents";
import { useCreateScheduledTask, useDeleteScheduledTask, useScheduledTasks } from "../api/scheduledTasks";

function agentLabel(agent: { friendly_name: string | null; name: string }) {
  return agent.friendly_name || agent.name;
}

export function ScheduledTasksPage() {
  const { data: agents } = useAgents();
  const { data: schedules } = useScheduledTasks();
  const createScheduledTask = useCreateScheduledTask();
  const deleteScheduledTask = useDeleteScheduledTask();
  const [searchParams] = useSearchParams();

  const requestedAgentId = searchParams.get("agentId") ?? "";
  const requestedAgent = useMemo(
    () => (agents ?? []).find((agent) => agent.id === requestedAgentId) ?? null,
    [agents, requestedAgentId],
  );

  const [scheduleAgentId, setScheduleAgentId] = useState("");
  const [scheduleName, setScheduleName] = useState("");
  const [cronExpression, setCronExpression] = useState("*/15 * * * *");
  const [schedulePrompt, setSchedulePrompt] = useState("");

  useEffect(() => {
    if (!agents?.length) {
      return;
    }
    setScheduleAgentId((current) => {
      if (requestedAgentId && agents.some((agent) => agent.id === requestedAgentId)) {
        return requestedAgentId;
      }
      return current || agents[0].id;
    });
  }, [agents, requestedAgentId]);

  const schedulesWithAgent = useMemo(
    () =>
      (schedules ?? []).map((schedule) => ({
        ...schedule,
        agent: (agents ?? []).find((agent) => agent.id === schedule.agent_id) ?? null,
      })),
    [agents, schedules],
  );

  const visibleSchedules = useMemo(
    () =>
      requestedAgentId
        ? schedulesWithAgent.filter((schedule) => schedule.agent_id === requestedAgentId)
        : schedulesWithAgent,
    [requestedAgentId, schedulesWithAgent],
  );

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await createScheduledTask.mutateAsync({
      agent_id: scheduleAgentId || agents?.[0]?.id,
      name: scheduleName,
      cron_expression: cronExpression,
      prompt: schedulePrompt,
      enabled: true,
    });
    setScheduleName("");
    setSchedulePrompt("");
  }

  async function onDelete(scheduleId: string, name: string) {
    const confirmed = window.confirm(`Delete scheduled task "${name}"?`);
    if (!confirmed) {
      return;
    }
    await deleteScheduledTask.mutateAsync(scheduleId);
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[0.72fr_1.28fr]">
      <form className="panel-frame p-6" onSubmit={onSubmit}>
        <div className="space-y-3">
          <p className="panel-label">Scheduler</p>
          <h2 className="text-3xl text-[var(--text-display)]">Timed tasks</h2>
          <p className="text-sm leading-6 text-[var(--text-secondary)]">
            Create recurring prompts for any agent. This is the place for “cada 15 minutos”, not `Comms`.
          </p>
          {requestedAgent ? (
            <p className="panel-inline-status">
              [FILTER] creating schedules for {agentLabel(requestedAgent)}
            </p>
          ) : null}
        </div>

        <div className="mt-8 space-y-5">
          <label className="panel-field">
            <span className="panel-label">Agent</span>
            <select value={scheduleAgentId} onChange={(event) => setScheduleAgentId(event.target.value)}>
              <option value="">Select runtime</option>
              {(agents ?? []).map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agentLabel(agent)}
                </option>
              ))}
            </select>
          </label>
          <label className="panel-field">
            <span className="panel-label">Name</span>
            <input value={scheduleName} onChange={(event) => setScheduleName(event.target.value)} />
          </label>
          <label className="panel-field">
            <span className="panel-label">Cron</span>
            <input value={cronExpression} onChange={(event) => setCronExpression(event.target.value)} />
          </label>
          <label className="panel-field">
            <span className="panel-label">Prompt</span>
            <textarea rows={6} value={schedulePrompt} onChange={(event) => setSchedulePrompt(event.target.value)} />
          </label>
          <button type="submit" className="panel-button-primary w-full" disabled={createScheduledTask.isPending}>
            {createScheduledTask.isPending ? "[LOADING]" : "Create schedule"}
          </button>
        </div>
      </form>

      <section className="panel-frame p-6">
        <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
          <div>
            <p className="panel-label">Active schedules</p>
            <h2 className="mt-2 text-3xl text-[var(--text-display)]">Recurring dispatch</h2>
          </div>
          <p className="panel-label">{visibleSchedules.length} configured</p>
        </div>

        <div className="mt-2">
          {visibleSchedules.length ? (
            visibleSchedules.map((schedule) => (
              <article key={schedule.id} className="grid gap-5 border-b border-[var(--border)] py-5 xl:grid-cols-[1fr_auto]">
                <div className="grid gap-5 md:grid-cols-[1fr_0.8fr]">
                  <div>
                    <p className="panel-label">{schedule.cron_expression}</p>
                    <p className="mt-2 text-xl text-[var(--text-display)]">{schedule.name}</p>
                    <p className="mt-2 text-sm text-[var(--text-secondary)]">{schedule.prompt}</p>
                  </div>
                  <div>
                    <p className="panel-label">Agent</p>
                    <p className="mt-2 text-sm text-[var(--text-display)]">
                      {schedule.agent ? agentLabel(schedule.agent) : schedule.agent_id}
                    </p>
                    <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                      next {schedule.next_run ? new Date(schedule.next_run).toLocaleString() : "pending"}
                    </p>
                    <p className="mt-1 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                      last {schedule.last_run ? new Date(schedule.last_run).toLocaleString() : "never"}
                    </p>
                  </div>
                </div>
                <div className="grid min-w-[14rem] gap-2">
                  {schedule.agent ? (
                    <Link className="panel-button-secondary w-full text-center" to={`/agents/${schedule.agent.id}`}>
                      Open agent
                    </Link>
                  ) : null}
                  <button
                    type="button"
                    className="panel-button-secondary w-full border-[var(--accent)] text-[var(--accent)]"
                    onClick={() => onDelete(schedule.id, schedule.name)}
                    disabled={deleteScheduledTask.isPending}
                  >
                    Delete schedule
                  </button>
                </div>
              </article>
            ))
          ) : (
            <p className="panel-inline-status mt-4">
              [EMPTY] no recurring tasks configured for this scope
            </p>
          )}
        </div>
      </section>
    </div>
  );
}

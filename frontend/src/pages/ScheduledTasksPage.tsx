import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useAgents } from "../api/agents";
import {
  useApproveScheduledTask,
  useCreateScheduledTask,
  useDeleteScheduledTask,
  useRunScheduledTaskNow,
  useScheduledTasks,
  useUpdateScheduledTask,
} from "../api/scheduledTasks";
import { describeCron, isValidCron } from "../lib/cron";
import { useI18n } from "../lib/i18n";
import { useSessionStore } from "../stores/sessionStore";
import type { ScheduledTask } from "../types/api";

function agentLabel(agent: { friendly_name: string | null; name: string }) {
  return agent.friendly_name || agent.name;
}

interface EditingSchedule {
  id: string;
  name: string;
  cron_expression: string;
  prompt: string;
}

export function ScheduledTasksPage() {
  const { t, formatDateTime, locale } = useI18n();
  const { data: agents } = useAgents();
  const { data: schedules } = useScheduledTasks();
  const createScheduledTask = useCreateScheduledTask();
  const deleteScheduledTask = useDeleteScheduledTask();
  const updateScheduledTask = useUpdateScheduledTask();
  const runNow = useRunScheduledTaskNow();
  const approveScheduledTask = useApproveScheduledTask();
  const currentUser = useSessionStore((state) => state.user);
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
  const [editing, setEditing] = useState<EditingSchedule | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

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

  const cronPreview = useMemo(() => {
    if (!isValidCron(cronExpression)) return null;
    return describeCron(cronExpression, locale as "en" | "es");
  }, [cronExpression, locale]);

  const editingCronPreview = useMemo(() => {
    if (!editing || !isValidCron(editing.cron_expression)) return null;
    return describeCron(editing.cron_expression, locale as "en" | "es");
  }, [editing, locale]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    if (!isValidCron(cronExpression)) {
      setFormError(t("schedules.invalidCron"));
      return;
    }
    try {
      await createScheduledTask.mutateAsync({
        agent_id: scheduleAgentId || agents?.[0]?.id,
        name: scheduleName,
        cron_expression: cronExpression,
        prompt: schedulePrompt,
        enabled: true,
      });
      setScheduleName("");
      setSchedulePrompt("");
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "Schedule creation failed");
    }
  }

  async function onToggleEnabled(schedule: ScheduledTask) {
    try {
      await updateScheduledTask.mutateAsync({ id: schedule.id, enabled: !schedule.enabled });
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Update failed");
    }
  }

  async function onToggleNotify(schedule: ScheduledTask) {
    try {
      await updateScheduledTask.mutateAsync({ id: schedule.id, notify_public_chat: !schedule.notify_public_chat });
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Update failed");
    }
  }

  async function onApprove(schedule: ScheduledTask) {
    const zone = schedule.timezone === "UTC" ? "UTC" : t("schedules.chileTime");
    const confirmed = window.confirm(
      t("schedules.approveConfirm", {
        name: schedule.name,
        cron: schedule.cron_expression,
        zone,
        prompt: schedule.prompt,
      }),
    );
    if (!confirmed) {
      return;
    }
    try {
      await approveScheduledTask.mutateAsync(schedule);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Approval failed");
    }
  }

  async function onRunNow(scheduleId: string) {
    try {
      await runNow.mutateAsync(scheduleId);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Run failed");
    }
  }

  async function onSaveEdit() {
    if (!editing) return;
    if (!isValidCron(editing.cron_expression)) {
      window.alert(t("schedules.invalidCron"));
      return;
    }
    try {
      await updateScheduledTask.mutateAsync({
        id: editing.id,
        name: editing.name,
        cron_expression: editing.cron_expression,
        prompt: editing.prompt,
      });
      setEditing(null);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Update failed");
    }
  }

  async function onDelete(scheduleId: string, name: string) {
    const confirmed = window.confirm(`${t("schedules.delete")} "${name}"?`);
    if (!confirmed) {
      return;
    }
    try {
      await deleteScheduledTask.mutateAsync(scheduleId);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Schedule deletion failed");
    }
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[0.72fr_1.28fr]">
      <form className="panel-frame p-6" onSubmit={onSubmit}>
        <div className="space-y-3">
          <p className="panel-label">{t("schedules.scheduler")}</p>
          <h2 className="text-3xl text-[var(--text-display)]">{t("schedules.timedTasks")}</h2>
          <p className="text-sm leading-6 text-[var(--text-secondary)]">
            {t("schedules.description")}
          </p>
          {requestedAgent ? (
            <p className="panel-inline-status">
              {t("schedules.filtering", { agent: agentLabel(requestedAgent) })}
            </p>
          ) : null}
        </div>

        <div className="mt-8 space-y-5">
          <label className="panel-field">
            <span className="panel-label">{t("tasks.agent")}</span>
            <select value={scheduleAgentId} onChange={(event) => setScheduleAgentId(event.target.value)}>
              <option value="">{t("tasks.selectRuntime")}</option>
              {(agents ?? []).map((agent) => (
                <option key={agent.id} value={agent.id}>
                  {agentLabel(agent)}
                </option>
              ))}
            </select>
          </label>
          <label className="panel-field">
            <span className="panel-label">{t("schedules.name")}</span>
            <input value={scheduleName} onChange={(event) => setScheduleName(event.target.value)} required />
          </label>
          <label className="panel-field">
            <span className="panel-label">{t("schedules.cron")}</span>
            <input value={cronExpression} onChange={(event) => setCronExpression(event.target.value)} />
            <span className="mt-1 block text-xs text-[var(--text-disabled)]">{t("schedules.chileTime")}</span>
            {cronPreview ? (
              <span className="mt-1 block text-xs text-[var(--success)]">{cronPreview}</span>
            ) : cronExpression.trim() ? (
              <span className="mt-1 block text-xs text-[var(--danger)]">{t("schedules.invalidCron")}</span>
            ) : null}
          </label>
          <label className="panel-field">
            <span className="panel-label">{t("tasks.prompt")}</span>
            <textarea rows={6} value={schedulePrompt} onChange={(event) => setSchedulePrompt(event.target.value)} required />
          </label>
          {formError ? <p className="text-sm text-[var(--danger)]">{formError}</p> : null}
          <button type="submit" className="panel-button-primary w-full" disabled={createScheduledTask.isPending}>
            {createScheduledTask.isPending ? t("common.loading") : t("schedules.create")}
          </button>
        </div>
      </form>

      <section className="panel-frame p-6">
        <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
          <div>
            <p className="panel-label">{t("schedules.activeSchedules")}</p>
            <h2 className="mt-2 text-3xl text-[var(--text-display)]">{t("schedules.recurringDispatch")}</h2>
          </div>
          <p className="panel-label">{t("schedules.configured", { count: visibleSchedules.length })}</p>
        </div>

        <div className="mt-2">
          {visibleSchedules.length ? (
            visibleSchedules.map((schedule) => (
              <article key={schedule.id} className="grid gap-5 border-b border-[var(--border)] py-5 xl:grid-cols-[1fr_auto]">
                {editing?.id === schedule.id ? (
                  <div className="grid gap-4">
                    <label className="panel-field">
                      <span className="panel-label">{t("schedules.name")}</span>
                      <input
                        value={editing.name}
                        onChange={(event) => setEditing({ ...editing, name: event.target.value })}
                      />
                    </label>
                    <label className="panel-field">
                      <span className="panel-label">{t("schedules.cron")}</span>
                      <input
                        value={editing.cron_expression}
                        onChange={(event) => setEditing({ ...editing, cron_expression: event.target.value })}
                      />
                      {editingCronPreview ? (
                        <span className="mt-1 block text-xs text-[var(--success)]">{editingCronPreview}</span>
                      ) : null}
                    </label>
                    <label className="panel-field">
                      <span className="panel-label">{t("tasks.prompt")}</span>
                      <textarea
                        rows={4}
                        value={editing.prompt}
                        onChange={(event) => setEditing({ ...editing, prompt: event.target.value })}
                      />
                    </label>
                    <div className="flex gap-2">
                      <button
                        type="button"
                        className="panel-button-primary"
                        onClick={() => void onSaveEdit()}
                        disabled={updateScheduledTask.isPending}
                      >
                        {t("common.save")}
                      </button>
                      <button type="button" className="panel-button-secondary" onClick={() => setEditing(null)}>
                        {t("common.cancel")}
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                    <div className="grid gap-5 md:grid-cols-[1fr_0.8fr]">
                      <div>
                        <p className="panel-label">{schedule.cron_expression}</p>
                        <p className="mt-1 text-xs text-[var(--success)]">{describeCron(schedule.cron_expression, locale as "en" | "es")}</p>
                        <p className="mt-1 text-xs text-[var(--text-disabled)]">
                          {schedule.timezone === "UTC" ? "UTC" : t("schedules.chileTime")}
                        </p>
                        <p className="mt-2 text-xl text-[var(--text-display)]">{schedule.name}</p>
                        {schedule.approval_status === "pending" ? (
                          <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--danger)]">
                            {t("schedules.pendingApproval")}
                            {schedule.created_by_agent_id ? ` · ${t("schedules.requestedByAgent")}` : ""}
                          </p>
                        ) : null}
                        <p className="mt-2 whitespace-pre-wrap text-sm text-[var(--text-secondary)]">{schedule.prompt}</p>
                      </div>
                      <div>
                        <p className="panel-label">{t("tasks.agent")}</p>
                        <p className="mt-2 text-sm text-[var(--text-display)]">
                          {schedule.agent ? agentLabel(schedule.agent) : schedule.agent_id}
                        </p>
                        <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                          {t("schedules.next", { value: schedule.next_run ? formatDateTime(schedule.next_run) : t("schedules.pending") })}
                        </p>
                        <p className="mt-1 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                          {t("schedules.last", { value: schedule.last_run ? formatDateTime(schedule.last_run) : t("schedules.never") })}
                        </p>
                      </div>
                    </div>
                    <div className="grid min-w-[14rem] gap-2 content-start">
                      {schedule.approval_status === "pending" ? (
                        currentUser?.id === schedule.owner_user_id ? (
                          <button
                            type="button"
                            className="panel-button-primary w-full"
                            onClick={() => void onApprove(schedule)}
                            disabled={approveScheduledTask.isPending && approveScheduledTask.variables?.id === schedule.id}
                          >
                            {t("schedules.approve")}
                          </button>
                        ) : (
                          <p className="text-xs text-[var(--text-disabled)]">{t("schedules.awaitingOwner")}</p>
                        )
                      ) : (
                        <button
                          type="button"
                          className={`panel-button-secondary w-full ${schedule.enabled ? "border-[var(--success)] text-[var(--success)]" : "text-[var(--text-disabled)]"}`}
                          onClick={() => void onToggleEnabled(schedule)}
                          disabled={updateScheduledTask.isPending}
                        >
                          {schedule.enabled ? t("schedules.disable") : t("schedules.enable")}
                        </button>
                      )}
                      <button
                        type="button"
                        title={t("schedules.notifyHint")}
                        className={`panel-button-secondary w-full ${schedule.notify_public_chat ? "border-[var(--success)] text-[var(--success)]" : "text-[var(--text-disabled)]"}`}
                        onClick={() => void onToggleNotify(schedule)}
                        disabled={updateScheduledTask.isPending}
                      >
                        {schedule.notify_public_chat ? t("schedules.notifyOn") : t("schedules.notifyOff")}
                      </button>
                      <button
                        type="button"
                        className="panel-button-secondary w-full"
                        onClick={() => void onRunNow(schedule.id)}
                        disabled={schedule.approval_status === "pending" || (runNow.isPending && runNow.variables === schedule.id)}
                      >
                        {runNow.isPending && runNow.variables === schedule.id ? t("common.loading") : t("schedules.runNow")}
                      </button>
                      <button
                        type="button"
                        className="panel-button-secondary w-full"
                        onClick={() =>
                          setEditing({
                            id: schedule.id,
                            name: schedule.name,
                            cron_expression: schedule.cron_expression,
                            prompt: schedule.prompt,
                          })
                        }
                      >
                        {t("common.edit")}
                      </button>
                      {schedule.agent ? (
                        <Link className="panel-button-secondary w-full text-center" to={`/agents/${schedule.agent.id}`}>
                          {t("schedules.openAgent")}
                        </Link>
                      ) : null}
                      <button
                        type="button"
                        className="panel-button-secondary w-full border-[var(--danger)] text-[var(--danger)]"
                        onClick={() => onDelete(schedule.id, schedule.name)}
                        disabled={deleteScheduledTask.isPending && deleteScheduledTask.variables === schedule.id}
                      >
                        {t("schedules.delete")}
                      </button>
                    </div>
                  </>
                )}
              </article>
            ))
          ) : (
            <p className="panel-inline-status mt-4">
              {t("schedules.empty")}
            </p>
          )}
        </div>
      </section>
    </div>
  );
}

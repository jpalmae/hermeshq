import { FormEvent, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { useAgents } from "../../api/agents";
import {
  useApproveScheduledTask,
  useCreateScheduledTask,
  useDeleteScheduledTask,
  useRunScheduledTaskNow,
  useScheduledTasks,
  useUpdateScheduledTask,
} from "../../api/scheduledTasks";
import { describeCron, isValidCron } from "../../lib/cron";
import { v2toast, extractErrorMessage } from "../toast";
import type { ScheduledTask } from "../../types/api";
import { useI18n } from "../../lib/i18n";
import { useSessionStore } from "../../stores/sessionStore";

function agentLabel(agent: { friendly_name: string | null; name: string }) {
  return agent.friendly_name || agent.name;
}

interface EditingSchedule {
  id: string;
  name: string;
  cron_expression: string;
  prompt: string;
}

export function V2SchedulesPage() {
  const { t } = useI18n();
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

  const [scheduleAgentId, setScheduleAgentId] = useState("");
  const [scheduleName, setScheduleName] = useState("");
  const [cronExpression, setCronExpression] = useState("*/15 * * * *");
  const [schedulePrompt, setSchedulePrompt] = useState("");
  const [editing, setEditing] = useState<EditingSchedule | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    if (!agents?.length) return;
    setScheduleAgentId((current) => {
      if (requestedAgentId && agents.some((a) => a.id === requestedAgentId)) return requestedAgentId;
      return current || agents[0].id;
    });
  }, [agents, requestedAgentId]);

  const schedulesWithAgent = useMemo(
    () =>
      (schedules ?? []).map((schedule) => ({
        ...schedule,
        agent: (agents ?? []).find((a) => a.id === schedule.agent_id) ?? null,
      })),
    [agents, schedules],
  );

  const visibleSchedules = useMemo(
    () => (requestedAgentId ? schedulesWithAgent.filter((s) => s.agent_id === requestedAgentId) : schedulesWithAgent),
    [requestedAgentId, schedulesWithAgent],
  );

  const cronPreview = useMemo(() => (isValidCron(cronExpression) ? describeCron(cronExpression, "en") : null), [cronExpression]);
  const editingCronPreview = useMemo(
    () => (editing && isValidCron(editing.cron_expression) ? describeCron(editing.cron_expression, "en") : null),
    [editing],
  );

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    if (!isValidCron(cronExpression)) {
      setFormError(t("v2.invalidCron"));
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
      v2toast.success(t("v2.scheduleCreated"));
    } catch (error) {
      setFormError(extractErrorMessage(error, t("v2.scheduleCreationFailed")));
    }
  }

  async function onToggleEnabled(schedule: ScheduledTask) {
    try {
      await updateScheduledTask.mutateAsync({ id: schedule.id, enabled: !schedule.enabled });
      v2toast.success(schedule.enabled ? t("v2.scheduleDisabled") : t("v2.scheduleEnabled"));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.updateFailed")));
    }
  }

  async function onToggleNotify(schedule: ScheduledTask) {
    try {
      await updateScheduledTask.mutateAsync({ id: schedule.id, notify_public_chat: !schedule.notify_public_chat });
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.updateFailed")));
    }
  }

  async function onApprove(schedule: ScheduledTask) {
    const zone = schedule.timezone === "UTC" ? "UTC" : t("v2.chileTime");
    const confirmed = window.confirm(
      t("v2.approveConfirm", {
        name: schedule.name,
        cron: schedule.cron_expression,
        zone,
        prompt: schedule.prompt,
      }),
    );
    if (!confirmed) return;
    try {
      await approveScheduledTask.mutateAsync(schedule);
      v2toast.success(t("v2.scheduleEnabled"));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.updateFailed")));
    }
  }

  async function onRunNow(schedule: ScheduledTask) {
    try {
      await runNow.mutateAsync(schedule.id);
      v2toast.success(t("v2.scheduleDispatched", { name: schedule.name }));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.runFailed")));
    }
  }

  async function onSaveEdit() {
    if (!editing) return;
    if (!isValidCron(editing.cron_expression)) {
      v2toast.error(t("v2.invalidCron"));
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
      v2toast.success(t("v2.scheduleUpdated"));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.updateFailed")));
    }
  }

  async function onDelete(schedule: ScheduledTask) {
    try {
      await deleteScheduledTask.mutateAsync(schedule.id);
      v2toast.success(t("v2.scheduleDeleted", { name: schedule.name }));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.deleteFailed")));
    }
  }

  return (
    <div>
      <div className="v2-page-header">
        <div>
          <h1 className="v2-page-title">{t("v2.schedules")}</h1>
          <p className="v2-page-subtitle">{visibleSchedules.length} {t("v2.recurringTasks")}</p>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(300px, 380px) 1fr", gap: 20, alignItems: "start" }}>
        <form className="v2-card" onSubmit={onSubmit}>
          <div className="v2-card-header">
            <h2 className="v2-card-title">{t("v2.newSchedule")}</h2>
          </div>
          <div className="v2-card-body" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.agentField")}</label>
              <select className="v2-select" value={scheduleAgentId} onChange={(e) => setScheduleAgentId(e.target.value)}>
                <option value="">{t("v2.selectAgent")}</option>
                {(agents ?? []).map((agent) => (
                  <option key={agent.id} value={agent.id}>{agentLabel(agent)}</option>
                ))}
              </select>
            </div>
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.name")}</label>
              <input className="v2-input" value={scheduleName} onChange={(e) => setScheduleName(e.target.value)} required placeholder={t("v2.dailyNewsSummary")} />
            </div>
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.cronExpression")}</label>
              <input className="v2-input v2-mono" value={cronExpression} onChange={(e) => setCronExpression(e.target.value)} required />
              {cronPreview ? (
                <span className="v2-field-hint" style={{ color: "var(--v2-success)" }}>{cronPreview}</span>
              ) : cronExpression.trim() ? (
                <span className="v2-field-error">{t("v2.invalidCron")}</span>
              ) : (
                <span className="v2-field-hint">{t("v2.cronPlaceholder")}</span>
              )}
              <span className="v2-field-hint">{t("v2.chileTime")}</span>
            </div>
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.prompt")}</label>
              <textarea className="v2-textarea" rows={5} value={schedulePrompt} onChange={(e) => setSchedulePrompt(e.target.value)} required placeholder={t("v2.whatShouldAgentDoEach")} />
            </div>
            {formError ? <p className="v2-field-error">{formError}</p> : null}
            <button type="submit" className="v2-btn v2-btn-primary" disabled={createScheduledTask.isPending}>
              {createScheduledTask.isPending ? t("v2.creating") : t("v2.createSchedule")}
            </button>
          </div>
        </form>

        <section className="v2-card">
          <div className="v2-card-header">
            <h2 className="v2-card-title">{t("v2.activeSchedules")}</h2>
          </div>
          <div>
            {visibleSchedules.length === 0 ? (
              <div className="v2-empty">
                <p className="v2-empty-title">{t("v2.noSchedulesYet")}</p>
                <p className="v2-empty-text">{t("v2.recurringRunAutomatically")}</p>
              </div>
            ) : (
              visibleSchedules.map((schedule) => (
                <div key={schedule.id} style={{ padding: "16px 20px", borderBottom: "1px solid var(--v2-border)" }}>
                  {editing?.id === schedule.id ? (
                    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                      <div className="v2-field">
                        <label className="v2-field-label">{t("v2.name")}</label>
                        <input className="v2-input" value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
                      </div>
                      <div className="v2-field">
                        <label className="v2-field-label">{t("v2.cronShort")}</label>
                        <input className="v2-input v2-mono" value={editing.cron_expression} onChange={(e) => setEditing({ ...editing, cron_expression: e.target.value })} />
                        {editingCronPreview ? <span className="v2-field-hint" style={{ color: "var(--v2-success)" }}>{editingCronPreview}</span> : null}
                      </div>
                      <div className="v2-field">
                        <label className="v2-field-label">{t("v2.prompt")}</label>
                        <textarea className="v2-textarea" rows={4} value={editing.prompt} onChange={(e) => setEditing({ ...editing, prompt: e.target.value })} />
                      </div>
                      <div style={{ display: "flex", gap: 8 }}>
                        <button className="v2-btn v2-btn-primary" onClick={() => void onSaveEdit()} disabled={updateScheduledTask.isPending}>{t("v2.save")}</button>
                        <button className="v2-btn v2-btn-secondary" onClick={() => setEditing(null)}>{t("v2.cancel")}</button>
                      </div>
                    </div>
                  ) : (
                    <div style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                          <span style={{ fontWeight: 620, fontSize: 14.5 }}>{schedule.name}</span>
                          <span
                            className="v2-pill"
                            data-tone={schedule.approval_status === "pending" ? "warn" : schedule.enabled ? "success" : "neutral"}
                          >
                            <span className="v2-pill-dot" />
                            {schedule.approval_status === "pending"
                              ? t("v2.pendingApproval")
                              : schedule.enabled
                                ? t("v2.active")
                                : t("v2.paused")}
                          </span>
                        </div>
                        <div className="v2-mono" style={{ marginTop: 6, fontSize: 12, color: "var(--v2-text-secondary)" }}>
                          {schedule.cron_expression}
                          <span style={{ color: "var(--v2-success)", marginLeft: 8 }}>{describeCron(schedule.cron_expression, "en")}</span>
                          <span style={{ marginLeft: 8 }}>{schedule.timezone === "UTC" ? "UTC" : t("v2.chileTime")}</span>
                        </div>
                        <p
                          style={{
                            marginTop: 8,
                            fontSize: 13,
                            color: "var(--v2-text-secondary)",
                            ...(schedule.approval_status === "pending"
                              ? { whiteSpace: "pre-wrap" }
                              : { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }),
                          }}
                        >
                          {schedule.prompt}
                        </p>
                        {schedule.approval_status === "pending" && schedule.created_by_agent_id ? (
                          <p className="v2-field-hint">{t("v2.requestedByAgent")}</p>
                        ) : null}
                        <div className="v2-mono" style={{ marginTop: 8, fontSize: 11, color: "var(--v2-text-muted)" }}>
                          {schedule.agent ? agentLabel(schedule.agent) : schedule.agent_id.slice(0, 8)}
                          {schedule.next_run ? ` · ${t("v2.next")} ${new Date(schedule.next_run).toLocaleString()}` : ""}
                        </div>
                      </div>
                      <div style={{ display: "flex", flexDirection: "column", gap: 6, flexShrink: 0 }}>
                        <button className="v2-btn v2-btn-secondary" style={{ padding: "5px 12px", fontSize: 12 }} title={t("v2.notifyHint")} onClick={() => void onToggleNotify(schedule)} disabled={updateScheduledTask.isPending}>
                          {schedule.notify_public_chat ? t("v2.notifyOn") : t("v2.notifyOff")}
                        </button>
                        <button className="v2-btn v2-btn-secondary" style={{ padding: "5px 12px", fontSize: 12 }} onClick={() => void onRunNow(schedule)} disabled={schedule.approval_status === "pending" || (runNow.isPending && runNow.variables === schedule.id)}>
                          {runNow.isPending && runNow.variables === schedule.id ? t("v2.runningLoading") : t("v2.runNow")}
                        </button>
                        <button
                          className="v2-btn v2-btn-secondary"
                          style={{ padding: "5px 12px", fontSize: 12 }}
                          onClick={() => setEditing({ id: schedule.id, name: schedule.name, cron_expression: schedule.cron_expression, prompt: schedule.prompt })}
                        >
                          {t("v2.edit")}
                        </button>
                        {schedule.approval_status === "pending" ? (
                          currentUser?.id === schedule.owner_user_id ? (
                            <button className="v2-btn v2-btn-primary" style={{ padding: "5px 12px", fontSize: 12 }} onClick={() => void onApprove(schedule)} disabled={approveScheduledTask.isPending && approveScheduledTask.variables?.id === schedule.id}>
                              {t("v2.approve")}
                            </button>
                          ) : (
                            <span className="v2-field-hint">{t("v2.awaitingOwner")}</span>
                          )
                        ) : (
                          <button className="v2-btn v2-btn-secondary" style={{ padding: "5px 12px", fontSize: 12 }} onClick={() => void onToggleEnabled(schedule)} disabled={updateScheduledTask.isPending}>
                            {schedule.enabled ? t("v2.disable") : t("v2.enable")}
                          </button>
                        )}
                        <button className="v2-btn v2-btn-danger" style={{ padding: "5px 12px", fontSize: 12 }} onClick={() => void onDelete(schedule)} disabled={deleteScheduledTask.isPending && deleteScheduledTask.variables === schedule.id}>
                          {t("v2.delete")}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              ))
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

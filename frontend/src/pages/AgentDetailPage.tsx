import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useAgent, useAgentAction, useDeleteAgent, useDeleteAgentAvatar, useTestAgentIntegration, useUpdateAgent, useUploadAgentAvatar } from "../api/agents";
import { useLogs } from "../api/logs";
import { useManagedIntegrations } from "../api/managedIntegrations";
import { useRuntimeCapabilityOverview, useRuntimeProfiles } from "../api/runtimeProfiles";
import { useSecrets } from "../api/secrets";
import { useCreateTask, useTasks } from "../api/tasks";
import { AgentAvatar } from "../components/AgentAvatar";
import { AgentConversationPanel } from "../components/AgentConversationPanel";
import { AgentMessagingPanel } from "../components/AgentMessagingPanel";
import { AgentSkillsPanel } from "../components/AgentSkillsPanel";
import { AgentTerminal } from "../components/AgentTerminal";
import { WorkspacePanel } from "../components/WorkspacePanel";
import { useI18n } from "../lib/i18n";
import { useSessionStore } from "../stores/sessionStore";

const DEFAULT_SECTION_STATE = {
  configuration: false,
  conversation: true,
  integrations: false,
  skills: false,
  ledger: false,
  logs: false,
  workspace: false,
};

type ActivityEntry = Record<string, unknown>;

function asText(value: unknown) {
  return typeof value === "string" ? value : "";
}

function groupActivityEntries(entries: ActivityEntry[]) {
  const grouped: Array<ActivityEntry & { grouped_count?: number }> = [];
  let index = 0;

  while (index < entries.length) {
    const current = entries[index];
    const eventType = asText(current.event_type);
    const taskId = asText(current.task_id);
    if (eventType !== "agent.output" || !taskId) {
      grouped.push(current);
      index += 1;
      continue;
    }

    const run: ActivityEntry[] = [current];
    let nextIndex = index + 1;
    while (nextIndex < entries.length) {
      const candidate = entries[nextIndex];
      if (
        asText(candidate.event_type) === "agent.output"
        && asText(candidate.task_id) === taskId
      ) {
        run.push(candidate);
        nextIndex += 1;
        continue;
      }
      break;
    }

    if (run.length === 1) {
      grouped.push(current);
    } else {
      grouped.push({
        ...current,
        message: [...run].reverse().map((entry) => asText(entry.message)).join(""),
        grouped_count: run.length,
      });
    }
    index = nextIndex;
  }

  return grouped;
}

function statusTone(status: string) {
  if (status === "running") return "text-[var(--success)]";
  if (status === "stopped") return "text-[var(--text-secondary)]";
  return "text-[var(--warning)]";
}

function slugify(value: string) {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    || "agent";
}

export function AgentDetailPage() {
  const { agentId } = useParams();
  const navigate = useNavigate();
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";
  const { t, formatDateTime } = useI18n();
  const { data: agent, isLoading } = useAgent(agentId);
  const { data: tasks } = useTasks();
  const { data: logs } = useLogs(agentId);
  const { data: runtimeProfiles } = useRuntimeProfiles(Boolean(currentUser));
  const { data: runtimeCapabilityOverview } = useRuntimeCapabilityOverview(Boolean(currentUser));
  const { data: managedIntegrations } = useManagedIntegrations(Boolean(currentUser));
  const { data: secrets } = useSecrets(isAdmin);
  const startAgent = useAgentAction("start");
  const stopAgent = useAgentAction("stop");
  const deleteAgent = useDeleteAgent();
  const uploadAgentAvatar = useUploadAgentAvatar();
  const deleteAgentAvatar = useDeleteAgentAvatar();
  const testAgentIntegration = useTestAgentIntegration();
  const updateAgent = useUpdateAgent();
  const createTask = useCreateTask();
  const [identityForm, setIdentityForm] = useState({
    friendly_name: "",
    name: "",
    slug: "",
  });
  const [systemPromptDraft, setSystemPromptDraft] = useState("");
  const [runtimeProfileDraft, setRuntimeProfileDraft] = useState("standard");
  const [integrationDrafts, setIntegrationDrafts] = useState<Record<string, Record<string, string>>>({});
  const [integrationTestResults, setIntegrationTestResults] = useState<
    Record<string, { success: boolean; message: string; details?: Record<string, unknown> | null }>
  >({});
  const [sectionState, setSectionState] = useState(DEFAULT_SECTION_STATE);
  const [nameTouched, setNameTouched] = useState(false);
  const [slugTouched, setSlugTouched] = useState(false);
  const [ledgerQuery, setLedgerQuery] = useState("");
  const [activityQuery, setActivityQuery] = useState("");
  const agentTasks = useMemo(
    () => (tasks ?? []).filter((task) => task.agent_id === agentId),
    [tasks, agentId],
  );
  const ledgerTasks = useMemo(
    () =>
      [...agentTasks].sort(
        (left, right) => new Date(right.queued_at).getTime() - new Date(left.queued_at).getTime(),
      ),
    [agentTasks],
  );
  const filteredLedgerTasks = useMemo(() => {
    const query = ledgerQuery.trim().toLowerCase();
    if (!query) {
      return ledgerTasks;
    }
    return ledgerTasks.filter((task) =>
      [
        task.title,
        task.prompt,
        task.response,
        task.error_message,
        task.status,
        task.metadata ? JSON.stringify(task.metadata) : "",
      ]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query)),
    );
  }, [ledgerQuery, ledgerTasks]);
  const groupedActivityLogs = useMemo(
    () => groupActivityEntries((logs ?? []) as ActivityEntry[]),
    [logs],
  );
  const filteredActivityLogs = useMemo(() => {
    const query = activityQuery.trim().toLowerCase();
    if (!query) {
      return groupedActivityLogs;
    }
    return groupedActivityLogs.filter((entry) =>
      [
        entry.event_type,
        entry.message,
        entry.created_at,
        JSON.stringify(entry),
      ]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query)),
    );
  }, [activityQuery, groupedActivityLogs]);
  const selectedRuntimeProfile = useMemo(
    () => (runtimeProfiles ?? []).find((profile) => profile.slug === runtimeProfileDraft) ?? null,
    [runtimeProfileDraft, runtimeProfiles],
  );
  const currentRuntimeCapabilityProfile = useMemo(
    () => (runtimeCapabilityOverview?.profiles ?? []).find((profile) => profile.slug === (agent?.runtime_profile || "standard")) ?? null,
    [agent?.runtime_profile, runtimeCapabilityOverview],
  );
  const enabledManagedIntegrations = useMemo(
    () => (managedIntegrations ?? []).filter((integration) => Boolean(agent?.integration_configs?.[integration.slug])),
    [agent?.integration_configs, managedIntegrations],
  );
  const secretsByProvider = useMemo(() => {
    const map = new Map<string, typeof secrets>();
    for (const secret of secrets ?? []) {
      const providerKey = secret.provider || "__generic__";
      const bucket = map.get(providerKey) ?? [];
      bucket.push(secret);
      map.set(providerKey, bucket);
    }
    return map;
  }, [secrets]);

  useEffect(() => {
    if (!isLoading && agent === null) {
      navigate("/agents", { replace: true });
    }
  }, [agent, isLoading, navigate]);

  useEffect(() => {
    if (!agent) {
      return;
    }
    setIdentityForm({
      friendly_name: agent.friendly_name || agent.name,
      name: agent.name,
      slug: agent.slug,
    });
    setSystemPromptDraft(agent.system_prompt ?? "");
    setRuntimeProfileDraft(agent.runtime_profile || "standard");
    const nextIntegrationDrafts: Record<string, Record<string, string>> = {};
    for (const integration of managedIntegrations ?? []) {
      const currentConfig = (agent.integration_configs?.[integration.slug] as Record<string, unknown> | undefined) ?? {};
      nextIntegrationDrafts[integration.slug] = Object.fromEntries(
        integration.required_fields.map((field) => {
          const defaultValue = integration.defaults?.[field] ?? "";
          const currentValue = currentConfig[field];
          return [field, typeof currentValue === "string" ? currentValue : defaultValue];
        }),
      );
    }
    setIntegrationDrafts(nextIntegrationDrafts);
    setIntegrationTestResults({});
    setNameTouched(false);
    setSlugTouched(false);
  }, [agent, managedIntegrations]);

  useEffect(() => {
    if (!agentId) {
      return;
    }
    const raw = window.localStorage.getItem(`hermeshq.agentDetail.sections.${agentId}`);
    if (!raw) {
      setSectionState(DEFAULT_SECTION_STATE);
      return;
    }
    try {
      const parsed = JSON.parse(raw) as Partial<typeof DEFAULT_SECTION_STATE>;
      setSectionState({ ...DEFAULT_SECTION_STATE, ...parsed });
    } catch {
      setSectionState(DEFAULT_SECTION_STATE);
    }
  }, [agentId]);

  function toggleSection(section: keyof typeof DEFAULT_SECTION_STATE) {
    setSectionState((current) => {
      const next = { ...current, [section]: !current[section] };
      if (agentId) {
        window.localStorage.setItem(`hermeshq.agentDetail.sections.${agentId}`, JSON.stringify(next));
      }
      return next;
    });
  }

  function renderSectionShell(
    section: keyof typeof DEFAULT_SECTION_STATE,
    eyebrow: string,
    title: string,
    meta: string,
    children: ReactNode,
  ) {
    const isOpen = sectionState[section];
    return (
      <section className="panel-frame p-6">
        <button
          type="button"
          className="flex w-full items-end justify-between gap-4 border-b border-[var(--border)] pb-4 text-left"
          onClick={() => toggleSection(section)}
        >
          <div>
            <p className="panel-label">{eyebrow}</p>
            <h3 className="mt-2 text-2xl text-[var(--text-display)]">{title}</h3>
          </div>
          <div className="text-right">
            <p className="panel-label">{meta}</p>
            <p className="mt-2 font-mono text-xs uppercase tracking-[0.1em] text-[var(--text-secondary)]">
              {isOpen ? t("agent.collapse") : t("agent.expand")}
            </p>
          </div>
        </button>
        {isOpen ? <div className="mt-5">{children}</div> : null}
      </section>
    );
  }

  if (isLoading || !agent) {
    return <p className="panel-inline-status">{t("common.loading")} {t("agent.loadingProfile")}</p>;
  }

  const currentAgent = agent;

  async function onDelete() {
    const confirmed = window.confirm(t("agents.deleteConfirm", { name: currentAgent.name }));
    if (!confirmed) {
      return;
    }
    try {
      await deleteAgent.mutateAsync(currentAgent.id);
      navigate("/agents");
    } catch (error) {
      const message =
        error instanceof Error ? error.message : t("agents.deleteFailed");
      window.alert(message);
    }
  }

  async function onSendInstruction(prompt: string) {
    if (currentAgent.status !== "running") {
      await startAgent.mutateAsync(currentAgent.id);
    }
    await createTask.mutateAsync({
      agent_id: currentAgent.id,
      title: "Chat message",
      prompt,
      priority: 5,
      metadata: {
        conversation: true,
        source: "agent_conversation",
      },
    });
  }

  async function onSaveIdentity() {
    await updateAgent.mutateAsync({
      agentId: currentAgent.id,
      payload: {
        friendly_name: identityForm.friendly_name.trim() || currentAgent.name,
        name: identityForm.name.trim(),
        slug: identityForm.slug.trim(),
      },
    });
  }

  async function onSaveSystemPrompt() {
    await updateAgent.mutateAsync({
      agentId: currentAgent.id,
      payload: {
        system_prompt: systemPromptDraft.trim() || null,
      },
    });
  }

  async function onSaveRuntimeProfile() {
    await updateAgent.mutateAsync({
      agentId: currentAgent.id,
      payload: {
        runtime_profile: runtimeProfileDraft,
      },
    });
  }

  async function onSaveIntegration(integrationSlug: string) {
    const integration = (managedIntegrations ?? []).find((item) => item.slug === integrationSlug);
    if (!integration) {
      return;
    }
    const currentDraft = integrationDrafts[integrationSlug] ?? {};
    const normalizedConfig = Object.fromEntries(
      integration.required_fields.map((field) => [field, (currentDraft[field] ?? "").trim()]),
    );
    await updateAgent.mutateAsync({
      agentId: currentAgent.id,
      payload: {
        skills: integration.skill_identifier
          ? Array.from(new Set([...(currentAgent.skills ?? []), integration.skill_identifier]))
          : currentAgent.skills,
        integration_configs: {
          ...(currentAgent.integration_configs ?? {}),
          [integrationSlug]: normalizedConfig,
        },
      },
    });
  }

  async function onDisableIntegration(integrationSlug: string) {
    const integration = (managedIntegrations ?? []).find((item) => item.slug === integrationSlug);
    if (!integration) {
      return;
    }
    const nextConfigs = { ...(currentAgent.integration_configs ?? {}) };
    delete nextConfigs[integrationSlug];
    await updateAgent.mutateAsync({
      agentId: currentAgent.id,
      payload: {
        skills: integration.skill_identifier
          ? (currentAgent.skills ?? []).filter((skill) => skill !== integration.skill_identifier)
          : currentAgent.skills,
        integration_configs: nextConfigs,
      },
    });
  }

  async function onTestIntegration(integrationSlug: string) {
    const currentDraft = integrationDrafts[integrationSlug] ?? {};
    const result = await testAgentIntegration.mutateAsync({
      agentId: currentAgent.id,
      integrationSlug,
      config: currentDraft,
    });
    setIntegrationTestResults((current) => ({
      ...current,
      [integrationSlug]: result,
    }));
  }

  async function onAvatarSelected(file: File | null) {
    if (!file) {
      return;
    }
    try {
      await uploadAgentAvatar.mutateAsync({ agentId: currentAgent.id, file });
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Avatar upload failed");
    }
  }

  async function onRemoveAvatar() {
    try {
      await deleteAgentAvatar.mutateAsync(currentAgent.id);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Avatar removal failed");
    }
  }

  return (
    <div className="space-y-6">
      <section className="grid gap-6 xl:grid-cols-[1.1fr_0.9fr]">
        <div className="panel-frame p-8">
          <p className="panel-label">{agent.slug}</p>
          <div className="mt-6 grid gap-8 md:grid-cols-[1fr_auto]">
            <div>
              <h2 className="text-[clamp(2.5rem,6vw,4.5rem)] leading-[0.95] text-[var(--text-display)]">
                {agent.friendly_name || agent.name}
              </h2>
              <p className="mt-3 text-sm uppercase tracking-[0.1em] text-[var(--text-secondary)]">
                {agent.name} / {agent.slug}
              </p>
              <p className="mt-4 max-w-[34rem] text-base leading-7 text-[var(--text-secondary)]">
                {agent.description ?? t("agent.noDescription")}
              </p>
            </div>
            <div className="flex flex-col items-end gap-3">
              <AgentAvatar agent={agent} sizeClass="h-28 w-28" />
              <div className="flex flex-wrap justify-end gap-2">
                <label className="panel-button-secondary cursor-pointer">
                  {t("agent.uploadAvatar")}
                  <input
                    className="hidden"
                    type="file"
                    accept="image/png,image/jpeg,image/webp"
                    onChange={(event) => void onAvatarSelected(event.target.files?.[0] ?? null)}
                  />
                </label>
                <button
                  type="button"
                  className="panel-button-secondary"
                  onClick={() => void onRemoveAvatar()}
                  disabled={!agent.has_avatar || deleteAgentAvatar.isPending}
                >
                  {t("agent.remove")}
                </button>
              </div>
            </div>
          </div>

          <div className="mt-10 grid gap-6 border-t border-[var(--border)] pt-6 md:grid-cols-4">
            <div>
              <p className="panel-label">{t("dashboard.status")}</p>
              <p className={`mt-2 text-lg uppercase tracking-[0.1em] ${statusTone(agent.status)}`}>
                {agent.status}
              </p>
            </div>
            <div>
              <p className="panel-label">{t("agent.mode")}</p>
              <p className="mt-2 text-lg text-[var(--text-display)]">{agent.run_mode}</p>
            </div>
            <div>
              <p className="panel-label">{t("dashboard.tasks")}</p>
              <p className="mt-2 text-lg text-[var(--text-display)]">{agent.total_tasks}</p>
            </div>
            <div>
              <p className="panel-label">{t("agent.tokens")}</p>
              <p className="mt-2 text-lg text-[var(--text-display)]">{agent.total_tokens_used}</p>
            </div>
          </div>

          <div className="mt-8 flex flex-wrap gap-3">
            <button className="panel-button-primary" onClick={() => startAgent.mutate(agent.id)}>
              {t("agent.startRuntime")}
            </button>
            <button className="panel-button-secondary" onClick={() => stopAgent.mutate(agent.id)}>
              {t("agent.stopRuntime")}
            </button>
            <Link className="panel-button-secondary" to={`/schedules?agentId=${agent.id}`}>
              {t("nav.schedules")}
            </Link>
            {isAdmin ? (
              <button
                className="panel-button-secondary border-[var(--accent)] text-[var(--accent)]"
                onClick={onDelete}
                disabled={deleteAgent.isPending}
              >
                {t("agent.delete")}
              </button>
            ) : null}
          </div>
        </div>

        <section className="panel-frame p-6">
          <button
            type="button"
            className="flex w-full items-end justify-between gap-4 border-b border-[var(--border)] pb-4 text-left"
            onClick={() => toggleSection("configuration")}
          >
            <div>
              <p className="panel-label">{t("agent.configuration")}</p>
              <h3 className="mt-2 text-2xl text-[var(--text-display)]">{t("agent.runtimeSettings")}</h3>
            </div>
            <div className="text-right">
              <p className="panel-label">{agent.provider} / {agent.model}</p>
              <p className="mt-2 font-mono text-xs uppercase tracking-[0.1em] text-[var(--text-secondary)]">
                {sectionState.configuration ? "Collapse" : "Expand"}
              </p>
            </div>
          </button>
          {sectionState.configuration ? (
            <div className="mt-5">
              <div className="border-b border-[var(--border)] pb-5">
                <label className="panel-field">
                  <span className="panel-label">{t("agents.friendlyName")}</span>
                  <input
                    value={identityForm.friendly_name}
                    onChange={(event) =>
                      setIdentityForm((current) => {
                        const friendlyName = event.target.value;
                        const next = { ...current, friendly_name: friendlyName };
                        if (!nameTouched) {
                          next.name = friendlyName.trim();
                        }
                        if (!slugTouched) {
                          next.slug = slugify(friendlyName.trim() || next.name.trim());
                        }
                        return next;
                      })
                    }
                    placeholder={t("agent.displayNameHumans")}
                  />
                </label>
                <label className="panel-field mt-4">
                  <span className="panel-label">{t("agent.technicalName")}</span>
                  <input
                    value={identityForm.name}
                    onChange={(event) => {
                      const nextName = event.target.value;
                      setNameTouched(true);
                      setIdentityForm((current) => {
                        const next = { ...current, name: nextName };
                        if (!slugTouched && !current.friendly_name.trim()) {
                          next.slug = slugify(nextName.trim());
                        }
                        return next;
                      });
                    }}
                    placeholder={t("agent.runtimeName")}
                  />
                </label>
                <label className="panel-field mt-4">
                  <span className="panel-label">{t("agents.slug")}</span>
                  <input
                    value={identityForm.slug}
                    onChange={(event) => {
                      setSlugTouched(true);
                      setIdentityForm((current) => ({ ...current, slug: event.target.value }));
                    }}
                    placeholder={t("agent.uniqueIdentifier")}
                  />
                </label>
                <div className="mt-4 flex items-center gap-3">
                  <button
                    type="button"
                    className="panel-button-secondary"
                    disabled={updateAgent.isPending}
                    onClick={onSaveIdentity}
                  >
                    {updateAgent.isPending ? t("common.loading") : t("agent.saveIdentity")}
                  </button>
                  <p className="panel-inline-status">{t("agent.identityHint")}</p>
                </div>
              </div>
              <div className="mt-6 space-y-4">
                <div className="border-b border-[var(--border)] pb-5">
                  <label className="panel-field">
                    <span className="panel-label">{t("agents.systemPrompt")}</span>
                    <textarea
                      rows={6}
                      value={systemPromptDraft}
                      onChange={(event) => setSystemPromptDraft(event.target.value)}
                      placeholder="Persistent operator instructions for this agent"
                    />
                  </label>
                  <div className="mt-4 flex items-center gap-3">
                    <button
                      type="button"
                      className="panel-button-secondary"
                      disabled={updateAgent.isPending}
                      onClick={onSaveSystemPrompt}
                    >
                      {updateAgent.isPending ? t("common.loading") : t("agent.saveSystemPrompt")}
                    </button>
                    <p className="panel-inline-status">{t("agent.systemPromptHint")}</p>
                  </div>
                </div>
                <div className="panel-stat-row">
                  <span>{t("agents.model")}</span>
                  <strong>{agent.model}</strong>
                </div>
                <div className="panel-stat-row">
                  <span>{t("agents.provider")}</span>
                  <strong>{agent.provider}</strong>
                </div>
                <div className="border-b border-[var(--border)] pb-5">
                  <label className="panel-field">
                    <span className="panel-label">{t("agents.runtimeProfile")}</span>
                    {isAdmin ? (
                      <select
                        value={runtimeProfileDraft}
                        onChange={(event) => setRuntimeProfileDraft(event.target.value)}
                      >
                        {(runtimeProfiles ?? []).map((profile) => (
                          <option key={profile.slug} value={profile.slug}>
                            {profile.name}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input value={selectedRuntimeProfile?.name ?? agent.runtime_profile} readOnly />
                    )}
                  </label>
                  {selectedRuntimeProfile ? (
                    <div className="mt-4 space-y-2 text-sm leading-6 text-[var(--text-secondary)]">
                      <p>{selectedRuntimeProfile.description}</p>
                      <p>{selectedRuntimeProfile.tooling_summary}</p>
                      <p className="text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                        {t("agents.profileFutureImage", { value: selectedRuntimeProfile.container_intent })}
                      </p>
                    </div>
                  ) : null}
                  {isAdmin ? (
                    <div className="mt-4 flex items-center gap-3">
                      <button
                        type="button"
                        className="panel-button-secondary"
                        disabled={updateAgent.isPending}
                        onClick={onSaveRuntimeProfile}
                      >
                        {updateAgent.isPending ? t("common.loading") : t("agent.saveRuntimeProfile")}
                      </button>
                      <p className="panel-inline-status">{t("agent.runtimeProfileHint")}</p>
                    </div>
                  ) : null}
                </div>
                <div className="panel-stat-row">
                  <span>{t("agents.secretRef")}</span>
                  <strong>{agent.api_key_ref ?? t("agent.none")}</strong>
                </div>
                <div className="panel-stat-row">
                  <span>{t("agents.node")}</span>
                  <strong>{agent.node?.name ?? t("agent.localRuntime")}</strong>
                </div>
                <div className="panel-stat-row">
                  <span>Workspace</span>
                  <strong className="truncate text-right">{agent.workspace_path}</strong>
                </div>

                <AgentMessagingPanel agentId={agent.id} isAdmin={isAdmin} />
              </div>
            </div>
          ) : null}
        </section>
      </section>

      <AgentTerminal agentId={agent.id} mode={agent.run_mode} runtimeProfile={agent.runtime_profile} />

      {renderSectionShell(
        "conversation",
        t("agent.conversation"),
        t("agent.talkToAgent"),
        agent.status === "running" ? t("agent.liveRuntime") : t("agent.autoStartOnSend"),
        <AgentConversationPanel
          tasks={agentTasks}
          agentStatus={agent.status}
          onSubmit={onSendInstruction}
          isSubmitting={createTask.isPending}
          embedded
        />,
      )}

      {renderSectionShell(
        "ledger",
        t("agent.taskHistory"),
        t("agent.runtimeLedger"),
        t("agent.records", { count: agentTasks.length }),
        <div className="mt-0">
          <label className="panel-field border-b border-[var(--border)] pb-4">
            <span className="panel-label">{t("agent.searchRuntimeLedger")}</span>
            <input
              value={ledgerQuery}
              onChange={(event) => setLedgerQuery(event.target.value)}
              placeholder={t("agent.searchRuntimeLedgerPlaceholder")}
            />
          </label>
          {filteredLedgerTasks.length ? (
            filteredLedgerTasks.map((task) => (
              <article key={task.id} className="grid gap-4 border-b border-[var(--border)] py-5 md:grid-cols-[0.7fr_1.3fr]">
                <div>
                  <p className="panel-label">{task.status}</p>
                  <p className="mt-2 text-sm text-[var(--text-primary)]">
                    {task.title ?? t("tasks.operatorTask")}
                  </p>
                  <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                    {formatDateTime(task.queued_at)}
                  </p>
                </div>
                <div>
                  <p className="text-sm leading-6 text-[var(--text-secondary)]">{task.prompt}</p>
                  {task.response ? (
                    <pre className="mt-3 overflow-x-auto whitespace-pre-wrap border-t border-[var(--border)] pt-3 text-sm leading-6 text-[var(--text-primary)]">
                      {task.response}
                    </pre>
                  ) : null}
                  {task.error_message ? (
                    <p className="mt-3 border-t border-[var(--border)] pt-3 text-sm leading-6 text-[var(--danger)]">
                      {task.error_message}
                    </p>
                  ) : null}
                </div>
              </article>
            ))
          ) : (
            <p className="panel-inline-status pt-5">{t("agent.noRuntimeLedgerMatches")}</p>
          )}
        </div>,
      )}

      {renderSectionShell(
        "integrations",
        t("agent.integrations"),
        t("agent.integrationRegistry"),
        t("agent.availableCount", { count: managedIntegrations?.length ?? 0 }),
        <div className="space-y-5">
          <article className="border border-[var(--border)] bg-[var(--surface-raised)] p-5">
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div>
                <p className="panel-label">{t("agent.effectiveCapabilities")}</p>
                <h4 className="mt-2 text-xl text-[var(--text-display)]">
                  {currentRuntimeCapabilityProfile?.name ?? agent.runtime_profile}
                </h4>
              </div>
              <span className="rounded-full border border-[var(--border)] px-3 py-1 text-xs text-[var(--text-secondary)]">
                {currentRuntimeCapabilityProfile?.terminal_allowed ? t("agent.terminalEnabled") : t("agent.terminalDisabled")}
              </span>
            </div>
            {currentRuntimeCapabilityProfile ? (
              <div className="mt-4 grid gap-6 lg:grid-cols-3">
                <div>
                  <p className="panel-label">{t("agent.profileBuiltins")}</p>
                  <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                    {currentRuntimeCapabilityProfile.tooling_summary}
                  </p>
                  {currentRuntimeCapabilityProfile.phase1_full_access ? (
                    <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                      {t("agent.phase1FullAccess")}
                    </p>
                  ) : (
                    <div className="mt-4 flex flex-wrap gap-2">
                      {currentRuntimeCapabilityProfile.builtin_toolsets.map((toolset) => (
                        <span
                          key={toolset.slug}
                          title={toolset.description}
                          className="rounded-full border border-[var(--border)] px-3 py-1 font-mono text-xs text-[var(--text-secondary)]"
                        >
                          {toolset.slug}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                <div>
                  <p className="panel-label">{t("agent.platformBuiltins")}</p>
                  <div className="mt-3 space-y-3 text-sm text-[var(--text-secondary)]">
                    {(runtimeCapabilityOverview?.platform_plugins ?? []).map((plugin) => (
                      <div key={plugin.slug} className="border-b border-[var(--border)] pb-3 last:border-b-0 last:pb-0">
                        <p className="text-[var(--text-primary)]">{plugin.name}</p>
                        <p className="mt-1 font-mono text-xs">{plugin.toolset}</p>
                        <p className="mt-2 leading-6">{plugin.description}</p>
                      </div>
                    ))}
                  </div>
                </div>
                <div>
                  <p className="panel-label">{t("agent.enabledIntegrationPackages")}</p>
                  {enabledManagedIntegrations.length ? (
                    <div className="mt-3 space-y-3 text-sm text-[var(--text-secondary)]">
                      {enabledManagedIntegrations.map((integration) => (
                        <div key={integration.slug} className="border-b border-[var(--border)] pb-3 last:border-b-0 last:pb-0">
                          <p className="text-[var(--text-primary)]">{integration.name}</p>
                          <p className="mt-1 font-mono text-xs">{integration.plugin_slug ?? integration.slug}</p>
                          <div className="mt-2 flex flex-wrap gap-2">
                            {integration.tools.map((tool) => (
                              <span
                                key={tool}
                                className="rounded-full border border-[var(--border)] px-3 py-1 font-mono text-xs text-[var(--text-secondary)]"
                              >
                                {tool}
                              </span>
                            ))}
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                      {t("agent.noEnabledIntegrationPackages")}
                    </p>
                  )}
                </div>
              </div>
            ) : null}
          </article>
          {(managedIntegrations ?? []).length ? (
            (managedIntegrations ?? []).map((integration) => {
              const enabled = Boolean(currentAgent.integration_configs?.[integration.slug]);
              const draft = integrationDrafts[integration.slug] ?? {};
              const testResult = integrationTestResults[integration.slug];
              return (
                <article key={integration.slug} className="border border-[var(--border)] bg-[var(--surface-raised)] p-5">
                  <div className="flex flex-wrap items-start justify-between gap-4">
                    <div>
                      <p className="panel-label">{integration.slug}</p>
                      <h4 className="mt-2 text-xl text-[var(--text-display)]">{integration.name}</h4>
                      <p className="mt-2 max-w-[48rem] text-sm leading-6 text-[var(--text-secondary)]">
                        {integration.description}
                      </p>
                    </div>
                    <div className="text-right">
                      <p className="panel-label">{enabled ? t("agent.enabled") : t("agent.disabled")}</p>
                      <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                        {integration.plugin_name ?? integration.plugin_slug ?? t("agent.none")}
                      </p>
                    </div>
                  </div>

                  <div className="mt-4 grid gap-6 md:grid-cols-2">
                    <div>
                      <p className="panel-label">{t("agent.integrationTools")}</p>
                      <div className="mt-3 flex flex-wrap gap-2">
                        {integration.tools.map((tool) => (
                          <span
                            key={tool}
                            className="rounded-full border border-[var(--border)] px-3 py-1 font-mono text-xs text-[var(--text-secondary)]"
                          >
                            {tool}
                          </span>
                        ))}
                      </div>
                    </div>
                    <div>
                      <p className="panel-label">{t("agent.integrationRequirements")}</p>
                      <div className="mt-3 space-y-2 text-sm text-[var(--text-secondary)]">
                        <p>{t("agent.integrationSkill", { value: integration.skill_identifier ?? t("agent.none") })}</p>
                        <p>{t("agent.integrationSecretProvider", { value: integration.secret_provider ?? t("agent.none") })}</p>
                        <p>{t("agent.integrationProfiles", { value: integration.supported_profiles.join(", ") })}</p>
                        <p>{t("agent.integrationFields", { value: integration.required_fields.join(", ") })}</p>
                      </div>
                    </div>
                  </div>

                  <div className="mt-5 grid gap-4 md:grid-cols-2">
                    {integration.fields.map((field) => {
                      const eligibleSecrets = [
                        ...(secretsByProvider.get(field.secret_provider || integration.secret_provider || "__generic__") ?? []),
                        ...(((field.secret_provider || integration.secret_provider) ? secretsByProvider.get("__generic__") : []) ?? []),
                      ];
                      return (
                        <label key={field.name} className="panel-field">
                          <span className="panel-label">
                            {field.kind === "secret_ref"
                              ? t("agent.integrationSecretRef")
                              : field.kind === "url"
                                ? t("agent.integrationBaseUrl")
                                : field.label}
                          </span>
                          {field.kind === "secret_ref" ? (
                            isAdmin ? (
                              <select
                                value={draft[field.name] ?? ""}
                                onChange={(event) =>
                                  setIntegrationDrafts((current) => ({
                                    ...current,
                                    [integration.slug]: {
                                      ...(current[integration.slug] ?? {}),
                                      [field.name]: event.target.value,
                                    },
                                  }))
                                }
                              >
                                <option value="">{t("agent.none")}</option>
                                {eligibleSecrets.map((secret) => (
                                  <option key={secret.id} value={secret.name}>
                                    {secret.name}
                                  </option>
                                ))}
                              </select>
                            ) : (
                              <input value={draft[field.name] || t("agent.none")} readOnly />
                            )
                          ) : (
                            <input
                              value={draft[field.name] ?? ""}
                              onChange={(event) =>
                                setIntegrationDrafts((current) => ({
                                  ...current,
                                  [integration.slug]: {
                                    ...(current[integration.slug] ?? {}),
                                    [field.name]: event.target.value,
                                  },
                                }))
                              }
                              readOnly={!isAdmin}
                              placeholder={field.placeholder ?? integration.defaults[field.name] ?? ""}
                            />
                          )}
                        </label>
                      );
                    })}
                  </div>

                  <p className="mt-4 text-sm leading-6 text-[var(--text-secondary)]">
                    {t("agent.integrationSaveHint")}
                  </p>

                  {isAdmin ? (
                    <div className="mt-4 flex flex-wrap items-center gap-3">
                      <button
                        type="button"
                        className="panel-button-secondary"
                        disabled={updateAgent.isPending}
                        onClick={() => void onSaveIntegration(integration.slug)}
                      >
                        {enabled ? t("agent.saveIntegration") : t("agent.enableIntegration")}
                      </button>
                      <button
                        type="button"
                        className="panel-button-secondary"
                        disabled={testAgentIntegration.isPending}
                        onClick={() => void onTestIntegration(integration.slug)}
                      >
                        {testAgentIntegration.isPending ? t("common.loading") : t("agent.testIntegration")}
                      </button>
                      {enabled ? (
                        <button
                          type="button"
                          className="panel-button-secondary"
                          disabled={updateAgent.isPending}
                          onClick={() => void onDisableIntegration(integration.slug)}
                        >
                          {t("agent.disableIntegration")}
                        </button>
                      ) : null}
                    </div>
                  ) : null}
                  {testResult ? (
                    <div className="mt-4 border-t border-[var(--border)] pt-4 text-sm">
                      <p className={testResult.success ? "text-[var(--success)]" : "text-[var(--danger)]"}>
                        {testResult.success ? t("agent.integrationTestPassed") : t("agent.integrationTestFailed")} {testResult.message}
                      </p>
                      {testResult.details ? (
                        <pre className="mt-3 overflow-x-auto whitespace-pre-wrap text-xs leading-6 text-[var(--text-secondary)]">
                          {JSON.stringify(testResult.details, null, 2)}
                        </pre>
                      ) : null}
                    </div>
                  ) : null}
                </article>
              );
            })
          ) : (
            <p className="panel-inline-status">{t("agent.emptyIntegrations")}</p>
          )}
        </div>,
      )}

      {renderSectionShell(
        "skills",
        t("agent.skills"),
        t("agent.skillRegistry"),
        `${agent.skills.length} assigned`,
        <AgentSkillsPanel agent={agent} embedded />,
      )}

      {renderSectionShell(
        "logs",
        t("agent.logs"),
        t("agent.activityStream"),
        t("agent.events", { count: logs?.length ?? 0 }),
        <div className="mt-0">
          <label className="panel-field border-b border-[var(--border)] pb-4">
            <span className="panel-label">{t("agent.searchActivityStream")}</span>
            <input
              value={activityQuery}
              onChange={(event) => setActivityQuery(event.target.value)}
              placeholder={t("agent.searchActivityStreamPlaceholder")}
            />
          </label>
          {filteredActivityLogs.length ? (
            filteredActivityLogs.map((entry) => (
              <article key={String(entry.id)} className="grid gap-3 border-b border-[var(--border)] py-4 md:grid-cols-[0.45fr_1.55fr]">
                <div>
                  <p className="panel-label">{String(entry.event_type)}</p>
                  <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                    {formatDateTime(String(entry.created_at))}
                  </p>
                  {typeof entry.grouped_count === "number" && entry.grouped_count > 1 ? (
                    <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                      {t("agent.groupedFragments", { count: entry.grouped_count })}
                    </p>
                  ) : null}
                </div>
                <div>
                  <p className="text-sm text-[var(--text-primary)]">{String(entry.message ?? "")}</p>
                </div>
              </article>
            ))
          ) : (
            <p className="panel-inline-status pt-5">{t("agent.noActivityStreamMatches")}</p>
          )}
        </div>,
      )}

      {renderSectionShell(
        "workspace",
        t("agent.workspace"),
        t("agent.filesystemEditor"),
        agent.workspace_path,
        <WorkspacePanel agentId={agent.id} />,
      )}
    </div>
  );
}

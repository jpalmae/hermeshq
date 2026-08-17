import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { useAgent, useAgentAction, useDeleteAgent, useDeleteAgentAvatar, useGenerateAIAgentAvatar, useGenerateAgentAvatar, useRunAgentIntegrationAction, useTestAgentIntegration, useUpdateAgent, useUploadAgentAvatar } from "../api/agents";
import { useHermesVersions } from "../api/hermesVersions";
import { useLogs } from "../api/logs";
import { useManagedIntegrations } from "../api/managedIntegrations";
import { useRuntimeLedger } from "../api/runtimeLedger";
import { useRuntimeCapabilityOverview, useRuntimeProfiles } from "../api/runtimeProfiles";
import { useProviders } from "../api/providers";
import { useSecrets } from "../api/secrets";
import { useCreateTask, useTasks } from "../api/tasks";
import { AgentM365ScopesPanel } from "../components/AgentM365ScopesPanel";
import { AgentSkillsPanel } from "../components/AgentSkillsPanel";
import { AgentTerminal } from "../components/AgentTerminal";
import { WorkspacePanel } from "../components/WorkspacePanel";
import { useI18n } from "../lib/i18n";
import { useSessionStore } from "../stores/sessionStore";
import { useRealtimeStore } from "../stores/realtimeStore";
import { ChannelsTab } from "./agent-detail/ChannelsTab";
import { ChatLedger } from "./agent-detail/ChatLedger";
import { HeroSection } from "./agent-detail/HeroSection";
import { LogsTab } from "./agent-detail/LogsTab";
import { OverviewTab } from "./agent-detail/OverviewTab";
import { SectionShell } from "./agent-detail/SectionShell";
import { useAgentDrafts } from "./agent-detail/useAgentDrafts";
import {
  DEFAULT_SECTION_STATE,
  groupActivityEntries,
  type SectionKey,
  type SectionState,
} from "./agent-detail/utils";

export function AgentDetailPage() {
  const { agentId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";
  const { t } = useI18n();
  const { data: agent, isLoading } = useAgent(agentId);
  const { data: tasks } = useTasks(agentId);
  const [activityQuery, setActivityQuery] = useState("");
  const {
    data: logs,
    fetchNextPage: fetchOlderLogs,
    hasNextPage: hasOlderLogs,
    isFetchingNextPage: isFetchingOlderLogs,
  } = useLogs(agentId, 100, activityQuery);
  const { data: runtimeLedger } = useRuntimeLedger(agentId);
  const { data: runtimeProfiles } = useRuntimeProfiles(Boolean(currentUser));
  const { data: hermesVersions } = useHermesVersions(Boolean(currentUser) && isAdmin);
  const { data: runtimeCapabilityOverview } = useRuntimeCapabilityOverview(Boolean(currentUser));
  const { data: managedIntegrations } = useManagedIntegrations(Boolean(currentUser));
  const { data: secrets } = useSecrets(isAdmin);
  const { data: providers } = useProviders(Boolean(currentUser));
  const startAgent = useAgentAction("start");
  const stopAgent = useAgentAction("stop");
  const deleteAgent = useDeleteAgent();
  const uploadAgentAvatar = useUploadAgentAvatar();
  const deleteAgentAvatar = useDeleteAgentAvatar();
  const generateAvatar = useGenerateAgentAvatar();
  const generateAIAvatar = useGenerateAIAgentAvatar();
  const testAgentIntegration = useTestAgentIntegration();
  const runAgentIntegrationAction = useRunAgentIntegrationAction();
  const [integrationPending, setIntegrationPending] = useState<Record<string, string | null>>({});
  const updateAgent = useUpdateAgent();
  const createTask = useCreateTask();
  const [sectionState, setSectionState] = useState<SectionState>(DEFAULT_SECTION_STATE);
  const [ledgerQuery, setLedgerQuery] = useState("");

  const drafts = useAgentDrafts(
    agent,
    managedIntegrations,
    hermesVersions,
    runtimeProfiles,
    runtimeCapabilityOverview,
    secrets,
  );

  const agentTasks = useMemo(() => tasks ?? [], [tasks]);
  const filteredLedgerEntries = useMemo(() => {
    const query = ledgerQuery.trim().toLowerCase();
    if (!query) {
      return runtimeLedger ?? [];
    }
    return (runtimeLedger ?? []).filter((entry) =>
      [
        entry.channel,
        entry.direction,
        entry.entry_type,
        entry.title,
        entry.content,
        entry.status,
        entry.counterpart_label,
        entry.counterpart_agent_id,
        entry.details ? JSON.stringify(entry.details) : "",
      ]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(query)),
    );
  }, [ledgerQuery, runtimeLedger]);
  const flatLogs = useMemo(
    () => (logs?.pages ?? []).flatMap((page) => page.items),
    [logs],
  );
  const groupedActivityLogs = useMemo(
    () => groupActivityEntries(flatLogs),
    [flatLogs],
  );

  useEffect(() => {
    if (!isLoading && agent === null) {
      navigate("/agents", { replace: true });
    }
  }, [agent, isLoading, navigate]);

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

  // Subscribe without re-rendering the page (the terminal remounts on render)
  useEffect(() => {
    if (!agentId) return;
    return useRealtimeStore.subscribe((state) => {
      const latest = state.events[0];
      if (latest?.type === "avatar.updated" && latest.agent_id === agentId) {
        void queryClient.refetchQueries({ queryKey: ["agents", agentId] });
      }
    });
  }, [agentId, queryClient]);

  function toggleSection(section: SectionKey) {
    setSectionState((current) => {
      const next = { ...current, [section]: !current[section] };
      if (agentId) {
        window.localStorage.setItem(`hermeshq.agentDetail.sections.${agentId}`, JSON.stringify(next));
      }
      return next;
    });
  }

  if (isLoading || !agent) {
    return <p className="panel-inline-status">{t("common.loading")} {t("agent.loadingProfile")}</p>;
  }

  const currentAgent = agent;
  const archived = currentAgent.is_archived;

  async function onDelete() {
    const confirmKey = archived ? "agents.permanentDeleteConfirm" : "agents.deleteConfirm";
    const failKey = archived ? "agents.permanentDeleteFailed" : "agents.deleteFailed";
    const confirmed = window.confirm(t(confirmKey, { name: currentAgent.name }));
    if (!confirmed) {
      return;
    }
    try {
      await deleteAgent.mutateAsync(currentAgent.id);
      navigate("/agents");
    } catch (error) {
      const message =
        error instanceof Error ? error.message : t(failKey);
      window.alert(message);
    }
  }

  async function onSendInstruction(prompt: string) {
    if (archived) {
      return;
    }
    try {
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
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Failed to send instruction");
    }
  }

  async function onSaveIdentity() {
    const name = drafts.identityForm.name.trim();
    const slug = drafts.identityForm.slug.trim();
    if (!name || !slug) {
      window.alert("Name and slug are required.");
      return;
    }
    await updateAgent.mutateAsync({
      agentId: currentAgent.id,
      payload: {
        friendly_name: drafts.identityForm.friendly_name.trim() || currentAgent.name,
        name,
        slug,
      },
    });
  }

  async function onSaveSystemPrompt() {
    await updateAgent.mutateAsync({
      agentId: currentAgent.id,
      payload: {
        system_prompt: drafts.systemPromptDraft.trim() || null,
      },
    });
  }

  async function onSaveRuntimeProfile() {
    await updateAgent.mutateAsync({
      agentId: currentAgent.id,
      payload: {
        runtime_profile: drafts.runtimeProfileDraft,
        hermes_version: drafts.hermesVersionDraft === "bundled" ? null : drafts.hermesVersionDraft,
        approval_mode: drafts.approvalModeDraft,
        tool_progress_mode: drafts.toolProgressModeDraft,
        gateway_notifications_mode: drafts.gatewayNotificationsModeDraft,
        use_provider_default: drafts.useProviderDefaultDraft,
        ...(drafts.useProviderDefaultDraft ? {} : { model: drafts.customModelDraft }),
        fallback_provider: drafts.fallbackDraft.provider,
        fallback_model: drafts.fallbackDraft.model,
        fallback_api_key_ref: drafts.fallbackDraft.api_key_ref,
        fallback_base_url: drafts.fallbackDraft.base_url,
        auxiliary_models: Object.keys(drafts.auxiliaryDraft).length > 0 ? drafts.auxiliaryDraft : null,
      },
    });
  }

  async function onSaveIntegration(integrationSlug: string) {
    const integration = (managedIntegrations ?? []).find((item) => item.slug === integrationSlug);
    if (!integration) return;
    const currentDraft = drafts.integrationDrafts[integrationSlug] ?? {};
    const normalizedConfig = Object.fromEntries(
      integration.fields
        .map((field) => [field.name, (currentDraft[field.name] ?? "").trim()] as const)
        .filter(([, value]) => value),
    );
    setIntegrationPending((p) => ({ ...p, [integrationSlug]: "save" }));
    try {
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
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Failed to save integration");
    } finally {
      setIntegrationPending((p) => ({ ...p, [integrationSlug]: null }));
    }
  }

  async function onDisableIntegration(integrationSlug: string) {
    const integration = (managedIntegrations ?? []).find((item) => item.slug === integrationSlug);
    if (!integration) return;
    const nextConfigs = { ...(currentAgent.integration_configs ?? {}) };
    delete nextConfigs[integrationSlug];
    setIntegrationPending((p) => ({ ...p, [integrationSlug]: "disable" }));
    try {
      await updateAgent.mutateAsync({
        agentId: currentAgent.id,
        payload: {
          skills: integration.skill_identifier
            ? (currentAgent.skills ?? []).filter((skill) => skill !== integration.skill_identifier)
            : currentAgent.skills,
          integration_configs: nextConfigs,
        },
      });
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Failed to disable integration");
    } finally {
      setIntegrationPending((p) => ({ ...p, [integrationSlug]: null }));
    }
  }

  async function onTestIntegration(integrationSlug: string) {
    setIntegrationPending((p) => ({ ...p, [integrationSlug]: "test" }));
    try {
      const result = await testAgentIntegration.mutateAsync({
        agentId: currentAgent.id,
        integrationSlug,
      });
      drafts.setIntegrationTestResults((current) => ({ ...current, [integrationSlug]: result }));
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Integration test failed");
    } finally {
      setIntegrationPending((p) => ({ ...p, [integrationSlug]: null }));
    }
  }

  async function onRunIntegrationAction(integrationSlug: string, actionSlug: string) {
    setIntegrationPending((p) => ({ ...p, [integrationSlug]: actionSlug }));
    try {
      const result = await runAgentIntegrationAction.mutateAsync({
        agentId: currentAgent.id,
        integrationSlug,
        actionSlug,
        arguments: {},
      });
      drafts.setIntegrationActionResults((current) => ({
        ...current,
        [integrationSlug]: { ...(current[integrationSlug] ?? {}), [actionSlug]: result },
      }));
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Integration action failed");
    } finally {
      setIntegrationPending((p) => ({ ...p, [integrationSlug]: null }));
    }
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

  async function onGenerateAvatar() {
    try {
      await generateAvatar.mutateAsync(currentAgent.id);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Avatar generation failed");
    }
  }

  async function onGenerateAIAvatar() {
    try {
      const result = await generateAIAvatar.mutateAsync(currentAgent.id);
      if (result.task_id) {
        window.alert(t("agent.avatarAISubmitted").replace("{taskId}", result.task_id));
      }
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "AI avatar generation failed");
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
    <div className="agent-detail-page space-y-6">
      <section className="grid gap-6 xl:grid-cols-[1.1fr_0.9fr]">
        <HeroSection
          agent={agent}
          archived={archived}
          isAdmin={isAdmin}
          startPending={startAgent.isPending}
          stopPending={stopAgent.isPending}
          deletePending={deleteAgent.isPending}
          generateAvatarPending={generateAvatar.isPending}
          generateAIAvatarPending={generateAIAvatar.isPending}
          removeAvatarPending={deleteAgentAvatar.isPending}
          onStart={() => startAgent.mutate(agent.id)}
          onStop={() => stopAgent.mutate(agent.id)}
          onDelete={onDelete}
          onGenerateAvatar={onGenerateAvatar}
          onGenerateAIAvatar={onGenerateAIAvatar}
          onAvatarSelected={onAvatarSelected}
          onRemoveAvatar={onRemoveAvatar}
        />
        <OverviewTab
          agent={agent}
          isAdmin={isAdmin}
          isOpen={sectionState.configuration}
          onToggle={() => toggleSection("configuration")}
          drafts={drafts}
          providers={providers}
          runtimeProfiles={runtimeProfiles}
          hermesVersions={hermesVersions}
          secrets={secrets}
          updatePending={updateAgent.isPending}
          onSaveIdentity={onSaveIdentity}
          onSaveSystemPrompt={onSaveSystemPrompt}
          onSaveRuntimeProfile={onSaveRuntimeProfile}
        />
      </section>

      <AgentTerminal agentId={agent.id} mode={agent.run_mode} runtimeProfile={agent.runtime_profile} archived={archived} />

      <ChatLedger
        agent={agent}
        archived={archived}
        agentTasks={agentTasks}
        onSendInstruction={onSendInstruction}
        createTaskPending={createTask.isPending}
        runtimeLedger={runtimeLedger}
        filteredLedgerEntries={filteredLedgerEntries}
        ledgerQuery={ledgerQuery}
        setLedgerQuery={setLedgerQuery}
        sectionState={sectionState}
        onToggleSection={toggleSection}
      />

      <ChannelsTab
        agent={agent}
        isAdmin={isAdmin}
        managedIntegrations={managedIntegrations}
        integrationDrafts={drafts.integrationDrafts}
        setIntegrationDrafts={drafts.setIntegrationDrafts}
        integrationTestResults={drafts.integrationTestResults}
        integrationActionResults={drafts.integrationActionResults}
        integrationPending={integrationPending}
        secretsByProvider={drafts.secretsByProvider}
        runtimeCapabilityOverview={runtimeCapabilityOverview}
        currentRuntimeCapabilityProfile={drafts.currentRuntimeCapabilityProfile}
        enabledManagedIntegrations={drafts.enabledManagedIntegrations}
        updatePending={updateAgent.isPending}
        onSaveIntegration={onSaveIntegration}
        onDisableIntegration={onDisableIntegration}
        onTestIntegration={onTestIntegration}
        onRunIntegrationAction={onRunIntegrationAction}
        sectionState={sectionState}
        onToggleSection={toggleSection}
      />

      <SectionShell
        eyebrow="Microsoft 365"
        title={t("agent.m365AgentPermissions")}
        meta=""
        isOpen={sectionState["m365-scopes"]}
        onToggle={() => toggleSection("m365-scopes")}
      >
        <AgentM365ScopesPanel agentId={agent.id} />
      </SectionShell>

      <SectionShell
        eyebrow={t("agent.skills")}
        title={t("agent.skillRegistry")}
        meta={`${agent.skills.length} assigned`}
        isOpen={sectionState.skills}
        onToggle={() => toggleSection("skills")}
      >
        <AgentSkillsPanel agent={agent} embedded />
      </SectionShell>

      <LogsTab
        flatLogs={flatLogs}
        groupedActivityLogs={groupedActivityLogs}
        activityQuery={activityQuery}
        setActivityQuery={setActivityQuery}
        hasOlderLogs={hasOlderLogs}
        isFetchingOlderLogs={isFetchingOlderLogs}
        fetchOlderLogs={fetchOlderLogs}
        sectionState={sectionState}
        onToggleSection={toggleSection}
      />

      <SectionShell
        eyebrow={t("agent.workspace")}
        title={t("agent.filesystemEditor")}
        meta={agent.workspace_path}
        isOpen={sectionState.workspace}
        onToggle={() => toggleSection("workspace")}
      >
        <WorkspacePanel agentId={agent.id} />
      </SectionShell>
    </div>
  );
}

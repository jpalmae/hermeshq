import { FormEvent, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { useAgentAction, useAgents, useBulkAgentMessage, useBulkAgentTask, useCreateAgent, useDeleteAgent } from "../api/agents";
import { AgentAvatar } from "../components/AgentAvatar";
import { useHermesVersions } from "../api/hermesVersions";
import { useNodes } from "../api/nodes";
import { useProviders } from "../api/providers";
import { useRuntimeProfiles } from "../api/runtimeProfiles";
import { useSecrets } from "../api/secrets";
import { useSettings } from "../api/settings";
import { useI18n } from "../lib/i18n";
import { applyProviderPreset, findMatchingProvider } from "../lib/providers";
import { useSessionStore } from "../stores/sessionStore";

const emptyForm = {
  node_id: "",
  name: "",
  friendly_name: "",
  slug: "",
  description: "",
  run_mode: "hybrid",
  runtime_profile: "standard",
  hermes_version: "bundled",
  model: "",
  provider: "",
  api_key_ref: "",
  base_url: "",
  system_prompt: "",
};

type BulkDialogMode = "task" | "message" | null;

function slugify(value: string) {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    || "agent";
}

function statusTone(status: string) {
  if (status === "running") return "text-[var(--success)]";
  if (status === "stopped") return "text-[var(--text-secondary)]";
  return "text-[var(--warning)]";
}

export function AgentsPage() {
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";
  const { t } = useI18n();
  const { data: nodes } = useNodes(isAdmin);
  const [showArchived, setShowArchived] = useState(false);
  const { data: agents } = useAgents(showArchived && isAdmin);
  const { data: settings } = useSettings(isAdmin);
  const { data: hermesVersions } = useHermesVersions(isAdmin);
  const { data: providers } = useProviders(Boolean(currentUser));
  const { data: runtimeProfiles } = useRuntimeProfiles(Boolean(currentUser));
  const { data: secrets } = useSecrets(isAdmin);
  const createAgent = useCreateAgent();
  const deleteAgent = useDeleteAgent();
  const startAgent = useAgentAction("start");
  const stopAgent = useAgentAction("stop");
  const restartAgent = useAgentAction("restart");
  const bulkTask = useBulkAgentTask();
  const bulkMessage = useBulkAgentMessage();

  const [form, setForm] = useState(emptyForm);
  const [nameTouched, setNameTouched] = useState(false);
  const [slugTouched, setSlugTouched] = useState(false);
  const [selectedProviderSlug, setSelectedProviderSlug] = useState("");
  const [selectedAgentIds, setSelectedAgentIds] = useState<string[]>([]);
  const [bulkDialogMode, setBulkDialogMode] = useState<BulkDialogMode>(null);
  const [bulkTaskTitle, setBulkTaskTitle] = useState("");
  const [bulkTaskPrompt, setBulkTaskPrompt] = useState("");
  const [bulkTaskPriority, setBulkTaskPriority] = useState("5");
  const [bulkTaskAutoStart, setBulkTaskAutoStart] = useState(true);
  const [bulkMessageText, setBulkMessageText] = useState("");
  const [bulkMessageAutoStart, setBulkMessageAutoStart] = useState(true);

  const activeNodeId = useMemo(() => nodes?.[0]?.id ?? "", [nodes]);
  const enabledProviders = useMemo(
    () => (providers ?? []).filter((provider) => provider.enabled),
    [providers],
  );
  const selectedProvider = useMemo(
    () => enabledProviders.find((provider) => provider.slug === selectedProviderSlug) ?? null,
    [enabledProviders, selectedProviderSlug],
  );
  const selectedRuntimeProfile = useMemo(
    () => (runtimeProfiles ?? []).find((profile) => profile.slug === form.runtime_profile) ?? null,
    [form.runtime_profile, runtimeProfiles],
  );
  const selectableAgents = useMemo(
    () => (agents ?? []).filter((agent) => !agent.is_archived),
    [agents],
  );
  const selectedAgents = useMemo(() => {
    const selectedSet = new Set(selectedAgentIds);
    return selectableAgents.filter((agent) => selectedSet.has(agent.id));
  }, [selectableAgents, selectedAgentIds]);
  const selectableVisibleIds = useMemo(
    () => selectableAgents.map((agent) => agent.id),
    [selectableAgents],
  );
  const allVisibleSelected = selectableVisibleIds.length > 0 && selectableVisibleIds.every((agentId) => selectedAgentIds.includes(agentId));
  const hasAnySelection = selectedAgents.length > 0;

  useEffect(() => {
    setForm((current) => {
      if (current.name || current.friendly_name || current.slug || current.system_prompt || current.description) {
        return current;
      }
      return {
        ...current,
        node_id: current.node_id || activeNodeId,
        runtime_profile: current.runtime_profile || "standard",
        hermes_version: current.hermes_version || "bundled",
        model: current.model || settings?.default_model || "",
        provider: current.provider || settings?.default_provider || "",
        api_key_ref: current.api_key_ref || settings?.default_api_key_ref || "",
        base_url: current.base_url || settings?.default_base_url || "",
      };
    });
  }, [activeNodeId, settings]);

  useEffect(() => {
    const match = findMatchingProvider(enabledProviders, form.provider || settings?.default_provider, form.base_url || settings?.default_base_url);
    setSelectedProviderSlug(match?.slug ?? "");
  }, [enabledProviders, form.provider, form.base_url, settings?.default_provider, settings?.default_base_url]);

  useEffect(() => {
    const validIds = new Set(selectableVisibleIds);
    setSelectedAgentIds((current) => current.filter((agentId) => validIds.has(agentId)));
  }, [selectableVisibleIds]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await createAgent.mutateAsync({
      ...form,
      node_id: form.node_id || activeNodeId,
      hermes_version: form.hermes_version === "bundled" ? null : form.hermes_version,
      enabled_toolsets: [],
      disabled_toolsets: [],
      skills: [],
      team_tags: [],
      soul_md: "# Soul\n\nOperations-first runtime.",
    });
    setForm({
      ...emptyForm,
      node_id: activeNodeId,
      runtime_profile: "standard",
      hermes_version: "bundled",
      model: settings?.default_model ?? "",
      provider: settings?.default_provider ?? "",
      api_key_ref: settings?.default_api_key_ref ?? "",
      base_url: settings?.default_base_url ?? "",
    });
    setNameTouched(false);
    setSlugTouched(false);
  }

  async function onDelete(agentId: string, agentName: string) {
    const confirmed = window.confirm(t("agents.deleteConfirm", { name: agentName }));
    if (!confirmed) {
      return;
    }
    try {
      await deleteAgent.mutateAsync(agentId);
    } catch (error) {
      const message =
        error instanceof Error ? error.message : t("agents.deleteFailed");
      window.alert(message);
    }
  }

  function toggleAgentSelection(agentId: string) {
    setSelectedAgentIds((current) =>
      current.includes(agentId) ? current.filter((value) => value !== agentId) : [...current, agentId],
    );
  }

  function toggleAllVisible() {
    setSelectedAgentIds((current) => {
      if (allVisibleSelected) {
        return current.filter((agentId) => !selectableVisibleIds.includes(agentId));
      }
      const next = new Set(current);
      selectableVisibleIds.forEach((agentId) => next.add(agentId));
      return Array.from(next);
    });
  }

  function closeBulkDialog() {
    setBulkDialogMode(null);
  }

  async function submitBulkTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const result = await bulkTask.mutateAsync({
        agent_ids: selectedAgents.map((agent) => agent.id),
        title: bulkTaskTitle.trim(),
        prompt: bulkTaskPrompt.trim(),
        priority: Number.parseInt(bulkTaskPriority, 10) || 5,
        auto_start_stopped: bulkTaskAutoStart,
      });
      window.alert(
        t("agents.bulkTaskSubmitted", {
          submitted: result.submitted,
          skipped: result.skipped,
        }),
      );
      setSelectedAgentIds([]);
      setBulkDialogMode(null);
      setBulkTaskTitle("");
      setBulkTaskPrompt("");
      setBulkTaskPriority("5");
      setBulkTaskAutoStart(true);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : t("agents.bulkTaskFailed"));
    }
  }

  async function submitBulkMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const result = await bulkMessage.mutateAsync({
        agent_ids: selectedAgents.map((agent) => agent.id),
        message: bulkMessageText.trim(),
        auto_start_stopped: bulkMessageAutoStart,
      });
      window.alert(
        t("agents.bulkMessageSubmitted", {
          submitted: result.submitted,
          skipped: result.skipped,
        }),
      );
      setSelectedAgentIds([]);
      setBulkDialogMode(null);
      setBulkMessageText("");
      setBulkMessageAutoStart(true);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : t("agents.bulkMessageFailed"));
    }
  }

  return (
    <div className="grid gap-6">
      <div className={`grid gap-6 ${isAdmin ? "xl:grid-cols-[0.72fr_1.28fr]" : ""}`}>
      {isAdmin ? (
      <section className="panel-frame p-6">
        <div className="space-y-3">
          <p className="panel-label">{t("agents.createAgent")}</p>
          <h2 className="text-3xl text-[var(--text-display)]">{t("agents.localRuntimeBootstrap")}</h2>
          <p className="text-sm leading-6 text-[var(--text-secondary)]">
            {t("agents.createDescription")}
          </p>
        </div>

        <form className="mt-8 space-y-5" onSubmit={onSubmit}>
          <label className="panel-field">
            <span className="panel-label">{t("agents.node")}</span>
            <select
              value={form.node_id || activeNodeId}
              onChange={(event) => setForm((current) => ({ ...current, node_id: event.target.value }))}
            >
              {(nodes ?? []).map((node) => (
                <option key={node.id} value={node.id}>
                  {node.name}
                </option>
              ))}
            </select>
          </label>

          <label className="panel-field">
            <span className="panel-label">{t("agents.friendlyName")}</span>
            <input
              value={form.friendly_name}
              onChange={(event) =>
                setForm((current) => {
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
              placeholder={t("agents.friendlyNamePlaceholder")}
            />
          </label>

          <label className="panel-field">
            <span className="panel-label">{t("agents.name")}</span>
            <input
              value={form.name}
              onChange={(event) => {
                const nextName = event.target.value;
                setNameTouched(true);
                setForm((current) => {
                  const next = { ...current, name: nextName };
                  if (!slugTouched && !current.friendly_name.trim()) {
                    next.slug = slugify(nextName.trim());
                  }
                  return next;
                });
              }}
              placeholder={t("agents.namePlaceholder")}
            />
          </label>

          <label className="panel-field">
            <span className="panel-label">{t("agents.slug")}</span>
            <input
              value={form.slug}
              onChange={(event) => {
                setSlugTouched(true);
                setForm((current) => ({ ...current, slug: event.target.value }));
              }}
            />
          </label>

          <label className="panel-field">
            <span className="panel-label">{t("agents.runtimeProfile")}</span>
            <select
              value={form.runtime_profile}
              onChange={(event) =>
                setForm((current) => ({ ...current, runtime_profile: event.target.value }))
              }
            >
              {(runtimeProfiles ?? []).map((profile) => (
                <option key={profile.slug} value={profile.slug}>
                  {profile.name}
                </option>
              ))}
            </select>
          </label>

          <label className="panel-field">
            <span className="panel-label">Hermes Agent version</span>
            <select
              value={form.hermes_version}
              onChange={(event) =>
                setForm((current) => ({ ...current, hermes_version: event.target.value }))
              }
            >
              <option value="bundled">Inherit default / bundled</option>
              {(hermesVersions ?? [])
                .filter((item) => item.version !== "bundled" && item.installed)
                .map((item) => (
                  <option key={item.version} value={item.version}>
                    {item.version === "bundled"
                      ? `Bundled runtime${item.detected_version ? ` (${item.detected_version})` : ""}`
                      : `${item.version}${item.detected_version ? ` (${item.detected_version})` : ""}`}
                  </option>
                ))}
            </select>
          </label>

          {selectedRuntimeProfile ? (
            <div className="border border-[var(--border)] bg-[var(--surface-raised)] p-4">
              <p className="panel-label">{t("agents.profileIntent")}</p>
              <p className="mt-2 text-sm leading-6 text-[var(--text-primary)]">
                {selectedRuntimeProfile.description}
              </p>
              <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                {selectedRuntimeProfile.tooling_summary}
              </p>
              <p className="mt-3 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                {t("agents.profileFutureImage", { value: selectedRuntimeProfile.container_intent })}
              </p>
            </div>
          ) : null}

          <label className="panel-field">
            <span className="panel-label">Provider preset</span>
            <select
              value={selectedProviderSlug}
              onChange={(event) => {
                const slug = event.target.value;
                setSelectedProviderSlug(slug);
                const provider = enabledProviders.find((item) => item.slug === slug);
                if (!provider) {
                  return;
                }
                const applied = applyProviderPreset(provider, form.api_key_ref);
                setForm((current) => ({
                  ...current,
                  provider: applied.provider,
                  model: applied.model,
                  base_url: applied.base_url,
                  api_key_ref: applied.api_key_ref,
                }));
              }}
            >
              <option value="">{t("providers.selectProviderPreset")}</option>
              {enabledProviders.map((provider) => (
                <option key={provider.slug} value={provider.slug}>
                  {provider.name}
                </option>
              ))}
            </select>
          </label>

          <label className="panel-field">
            <span className="panel-label">{t("agents.model")}</span>
            <input
              value={form.model}
              onChange={(event) => setForm((current) => ({ ...current, model: event.target.value }))}
              placeholder={settings?.default_model ?? "Uses global default"}
            />
          </label>

          <label className="panel-field">
            <span className="panel-label">{t("agents.provider")}</span>
            <input
              value={form.provider}
              onChange={(event) => setForm((current) => ({ ...current, provider: event.target.value }))}
              placeholder={settings?.default_provider ?? "Uses global default"}
            />
          </label>

          <label className="panel-field">
            <span className="panel-label">{t("agents.secretRef")}</span>
            <select
              value={form.api_key_ref}
              onChange={(event) => setForm((current) => ({ ...current, api_key_ref: event.target.value }))}
            >
              <option value="">{selectedProvider?.supports_secret_ref === false ? t("providers.oauthManaged") : (settings?.default_api_key_ref ?? t("providers.noSecret"))}</option>
              {(secrets ?? []).map((secret) => (
                <option key={String(secret.id)} value={String(secret.name)}>
                  {String(secret.name)}{secret.provider ? ` (${secret.provider})` : ""}
                </option>
              ))}
            </select>
          </label>

          <label className="panel-field">
            <span className="panel-label">{t("agents.baseUrl")}</span>
            <input
              value={form.base_url}
              onChange={(event) => setForm((current) => ({ ...current, base_url: event.target.value }))}
              placeholder={settings?.default_base_url ?? "Uses global default"}
              disabled={selectedProvider?.supports_custom_base_url === false}
            />
          </label>

          {selectedProvider ? (
            <p className="text-sm leading-6 text-[var(--text-secondary)]">{selectedProvider.description}</p>
          ) : null}

          <label className="panel-field">
            <span className="panel-label">{t("agents.systemPrompt")}</span>
            <textarea
              rows={4}
              value={form.system_prompt}
              onChange={(event) =>
                setForm((current) => ({ ...current, system_prompt: event.target.value }))
              }
            />
          </label>

          <button type="submit" className="panel-button-primary w-full" disabled={createAgent.isPending}>
            {createAgent.isPending ? t("common.loading") : t("agents.create")}
          </button>
        </form>
      </section>
      ) : null}

      <section className="panel-frame p-6">
        <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
          <div>
            <p className="panel-label">{t("agents.fleetInventory")}</p>
            <h2 className="mt-2 text-3xl text-[var(--text-display)]">{t("agents.agentMatrix")}</h2>
          </div>
          <div className="flex items-center gap-4">
            {isAdmin ? (
              <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
                <input
                  type="checkbox"
                  checked={showArchived}
                  onChange={(event) => setShowArchived(event.target.checked)}
                />
                <span>{t("agents.showArchived")}</span>
              </label>
            ) : null}
            <p className="panel-label">{t("agents.registered", { count: agents?.length ?? 0 })}</p>
          </div>
        </div>
        {hasAnySelection ? (
          <div className="agents-bulk-bar mt-4 flex flex-wrap items-center gap-3 border border-[var(--border)] bg-[var(--surface-raised)] p-3">
            <span className="panel-label">{t("agents.selectedCount", { count: selectedAgents.length })}</span>
            <button type="button" className="panel-button-secondary" onClick={() => setSelectedAgentIds([])}>
              {t("agents.clearSelection")}
            </button>
            <button
              type="button"
              className="panel-button-secondary"
              onClick={() => setBulkDialogMode("message")}
            >
              {t("agents.sendMessage")}
            </button>
            <button
              type="button"
              className="panel-button-primary"
              onClick={() => setBulkDialogMode("task")}
            >
              {t("agents.dispatchTask")}
            </button>
          </div>
        ) : null}
        <div className="mt-2">
          {(agents ?? []).length ? (
            <div className="flex items-center gap-3 border-b border-[var(--border)] py-3 text-sm text-[var(--text-secondary)]">
              <input
                type="checkbox"
                checked={allVisibleSelected}
                onChange={toggleAllVisible}
                disabled={!selectableVisibleIds.length}
              />
              <span>{t("agents.selectVisible")}</span>
            </div>
          ) : null}
          {(agents ?? []).map((agent) => (
            <article
              key={agent.id}
              className={`agents-row grid gap-5 border-b border-[var(--border)] py-5 xl:grid-cols-[1fr_auto] ${
                selectedAgentIds.includes(agent.id) ? "is-selected" : ""
              }`}
            >
              <div className="grid gap-5 md:grid-cols-[1.1fr_0.8fr_0.8fr]">
                <div className="flex items-start gap-4">
                  <input
                    type="checkbox"
                    className="agents-select-box mt-3"
                    checked={selectedAgentIds.includes(agent.id)}
                    onChange={() => toggleAgentSelection(agent.id)}
                    disabled={agent.is_archived}
                  />
                  <AgentAvatar agent={agent} sizeClass="h-14 w-14" className="shrink-0" />
                  <div>
                    <p className="panel-label">{agent.slug}</p>
                    <Link to={`/agents/${agent.id}`} className="mt-2 block text-xl text-[var(--text-display)]">
                      {agent.friendly_name || agent.name}
                    </Link>
                    {agent.is_system_agent ? (
                      <span className="mt-2 inline-flex rounded-full border border-[var(--border)] px-3 py-1 text-[11px] uppercase tracking-[0.12em] text-[var(--text-secondary)]">
                        system / {agent.system_scope ?? "operator"}
                      </span>
                    ) : null}
                    {agent.friendly_name && agent.friendly_name !== agent.name ? (
                      <p className="mt-2 text-sm text-[var(--text-primary)]">{agent.name}</p>
                    ) : null}
                    <p className="mt-3 text-sm text-[var(--text-secondary)]">{agent.description ?? "No description"}</p>
                  </div>
                </div>
                <div>
                  <p className="panel-label">Runtime</p>
                  <p className="mt-2 text-sm text-[var(--text-primary)]">{agent.model}</p>
                  <p className="mt-1 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                    {agent.provider} / {agent.runtime_profile}
                  </p>
                  {agent.is_archived ? (
                    <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--accent)]">
                      {t("agent.archived")}
                    </p>
                  ) : null}
                </div>
                <div>
                  <p className="panel-label">Status</p>
                  <p className={`mt-2 text-sm uppercase tracking-[0.1em] ${statusTone(agent.status)}`}>
                    {agent.status}
                  </p>
                  <p className="mt-1 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                    tasks {agent.total_tasks} / tokens {agent.total_tokens_used}
                  </p>
                </div>
              </div>
              <div className="grid min-w-[18rem] gap-2 md:grid-cols-2">
                <button className="panel-button-secondary w-full" onClick={() => startAgent.mutate(agent.id)} disabled={agent.is_archived}>
                  Start
                </button>
                <button className="panel-button-secondary w-full" onClick={() => stopAgent.mutate(agent.id)} disabled={agent.is_archived}>
                  Stop
                </button>
                <button className="panel-button-secondary w-full" onClick={() => restartAgent.mutate(agent.id)} disabled={agent.is_archived}>
                  Restart
                </button>
                {isAdmin ? (
                  <button
                    className="panel-button-secondary w-full border-[var(--accent)] text-[var(--accent)]"
                    onClick={() => onDelete(agent.id, agent.name)}
                    disabled={deleteAgent.isPending}
                  >
                    {t("agent.delete")}
                  </button>
                ) : null}
              </div>
            </article>
          ))}
        </div>
      </section>
      </div>
      {bulkDialogMode ? (
        <div className="fixed inset-0 z-[80] flex items-center justify-center bg-[var(--overlay)] p-4">
          <div className="panel-frame w-full max-w-2xl p-6">
            {bulkDialogMode === "task" ? (
              <form className="space-y-5" onSubmit={submitBulkTask}>
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="panel-label">{t("agents.bulkTaskLabel")}</p>
                    <h2 className="mt-2 text-2xl text-[var(--text-display)]">{t("agents.dispatchTask")}</h2>
                  </div>
                  <button type="button" className="panel-button-secondary" onClick={closeBulkDialog}>
                    {t("common.no")}
                  </button>
                </div>
                <label className="panel-field">
                  <span className="panel-label">{t("agents.bulkTaskTitle")}</span>
                  <input value={bulkTaskTitle} onChange={(event) => setBulkTaskTitle(event.target.value)} required />
                </label>
                <label className="panel-field">
                  <span className="panel-label">{t("agents.bulkTaskPrompt")}</span>
                  <textarea rows={6} value={bulkTaskPrompt} onChange={(event) => setBulkTaskPrompt(event.target.value)} required />
                </label>
                <label className="panel-field">
                  <span className="panel-label">{t("agents.bulkTaskPriority")}</span>
                  <input
                    type="number"
                    min={1}
                    max={100}
                    value={bulkTaskPriority}
                    onChange={(event) => setBulkTaskPriority(event.target.value)}
                  />
                </label>
                <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
                  <input
                    type="checkbox"
                    checked={bulkTaskAutoStart}
                    onChange={(event) => setBulkTaskAutoStart(event.target.checked)}
                  />
                  <span>{t("agents.autoStartStopped")}</span>
                </label>
                <div className="border border-[var(--border)] bg-[var(--surface-raised)] p-4">
                  <p className="panel-label">{t("agents.bulkTargets")}</p>
                  <p className="mt-2 text-sm text-[var(--text-secondary)]">
                    {selectedAgents.map((agent) => agent.friendly_name || agent.name).join(", ")}
                  </p>
                </div>
                <div className="flex justify-end gap-3">
                  <button type="button" className="panel-button-secondary" onClick={closeBulkDialog}>
                    {t("agents.cancelBulk")}
                  </button>
                  <button type="submit" className="panel-button-primary" disabled={bulkTask.isPending}>
                    {bulkTask.isPending ? t("common.loading") : t("agents.dispatchTask")}
                  </button>
                </div>
              </form>
            ) : (
              <form className="space-y-5" onSubmit={submitBulkMessage}>
                <div className="flex items-start justify-between gap-4">
                  <div>
                    <p className="panel-label">{t("agents.bulkMessageLabel")}</p>
                    <h2 className="mt-2 text-2xl text-[var(--text-display)]">{t("agents.sendMessage")}</h2>
                  </div>
                  <button type="button" className="panel-button-secondary" onClick={closeBulkDialog}>
                    {t("common.no")}
                  </button>
                </div>
                <label className="panel-field">
                  <span className="panel-label">{t("agents.bulkMessageBody")}</span>
                  <textarea rows={6} value={bulkMessageText} onChange={(event) => setBulkMessageText(event.target.value)} required />
                </label>
                <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
                  <input
                    type="checkbox"
                    checked={bulkMessageAutoStart}
                    onChange={(event) => setBulkMessageAutoStart(event.target.checked)}
                  />
                  <span>{t("agents.autoStartStopped")}</span>
                </label>
                <div className="border border-[var(--border)] bg-[var(--surface-raised)] p-4">
                  <p className="panel-label">{t("agents.bulkTargets")}</p>
                  <p className="mt-2 text-sm text-[var(--text-secondary)]">
                    {selectedAgents.map((agent) => agent.friendly_name || agent.name).join(", ")}
                  </p>
                </div>
                <div className="flex justify-end gap-3">
                  <button type="button" className="panel-button-secondary" onClick={closeBulkDialog}>
                    {t("agents.cancelBulk")}
                  </button>
                  <button type="submit" className="panel-button-primary" disabled={bulkMessage.isPending}>
                    {bulkMessage.isPending ? t("common.loading") : t("agents.sendMessage")}
                  </button>
                </div>
              </form>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

import { useState } from "react";
import { Link } from "react-router-dom";

import type { Agent, ProviderDefinition, Secret, HermesVersion, AuxiliaryModelEntry } from "../../types/api";
import type { UseMutationResult } from "@tanstack/react-query";
import { useUploadAgentAvatar, useGenerateAgentAvatar, useGenerateAIAgentAvatar, useDeleteAgentAvatar } from "../../api/agents";
import { usePermissionPolicies } from "../../api/permissionPolicies";
import { AgentAvatar } from "../../components/AgentAvatar";
import { v2toast, extractErrorMessage } from "../toast";
import { useI18n } from "../../lib/i18n";

export function V2AgentConfigTab({
  agent,
  isAdmin,
  providers,
  secrets,
  hermesVersions,
  updateAgent,
}: {
  agent: Agent;
  isAdmin: boolean;
  providers: ProviderDefinition[];
  secrets: Secret[];
  hermesVersions: HermesVersion[];
  updateAgent: UseMutationResult<Agent, Error, { agentId: string; payload: Record<string, unknown> }>;
}) {
  const { t } = useI18n();
  const [useProviderDefault, setUseProviderDefault] = useState(agent.use_provider_default);
  const [customModel, setCustomModel] = useState(agent.model ?? "");
  const [provider, setProvider] = useState(agent.provider ?? "");
  const [apiKeyRef, setApiKeyRef] = useState(agent.api_key_ref ?? "");
  const [baseUrl, setBaseUrl] = useState(agent.base_url ?? "");
  const [systemPrompt, setSystemPrompt] = useState(agent.system_prompt ?? "");
  const [friendlyName, setFriendlyName] = useState(agent.friendly_name ?? "");
  const [description, setDescription] = useState(agent.description ?? "");
  const [approvalMode, setApprovalMode] = useState(agent.approval_mode ?? "inherit");
  const [toolProgressMode, setToolProgressMode] = useState(agent.tool_progress_mode ?? "inherit");
  const [gatewayNotifMode, setGatewayNotifMode] = useState(agent.gateway_notifications_mode ?? "inherit");
  const [runtimeProfile, setRuntimeProfile] = useState(agent.runtime_profile ?? "standard");
  const [hermesVersion, setHermesVersion] = useState(agent.hermes_version || "");
  const [fbProvider, setFbProvider] = useState(agent.fallback_provider ?? "");
  const [fbModel, setFbModel] = useState(agent.fallback_model ?? "");
  const [fbKeyRef, setFbKeyRef] = useState(agent.fallback_api_key_ref ?? "");
  const [fbBaseUrl, setFbBaseUrl] = useState(agent.fallback_base_url ?? "");
  const [auxDraft, setAuxDraft] = useState<Record<string, AuxiliaryModelEntry>>(
    agent.auxiliary_models ?? {},
  );
  const uploadAvatar = useUploadAgentAvatar();
  const generateAvatar = useGenerateAgentAvatar();
  const generateAIAvatar = useGenerateAIAgentAvatar();
  const removeAvatar = useDeleteAgentAvatar();
  const { data: permissionPolicies } = usePermissionPolicies();
  const [runtimeType, setRuntimeType] = useState(agent.runtime_type ?? "hermes");
  const [permissionPolicyId, setPermissionPolicyId] = useState(agent.permission_policy_id ?? "");
  const [piTools, setPiTools] = useState(((agent.pi_config as Record<string, unknown> | null)?.tools as string[]) ?? ["read", "bash", "edit"]);
  const [piThinking, setPiThinking] = useState(((agent.pi_config as Record<string, unknown> | null)?.thinking_level as string) ?? "off");
  const AUX_TASKS = [
    { key: "vision", label: t("v2.vision") },
    { key: "compression", label: t("v2.compression") },
    { key: "web_extract", label: t("v2.webExtract") },
  ];

  const agentProvider =
    providers.find((p) => p.runtime_provider === provider && (p.available_models ?? []).length > 0) ??
    providers.find((p) => p.runtime_provider === agent.provider && (p.available_models ?? []).length > 0) ??
    providers.find((p) => p.slug === agent.provider);
  const availableModels = agentProvider?.available_models ?? [];
  const fbProviderDef = providers.find((p) => p.runtime_provider === fbProvider && (p.available_models ?? []).length > 0);

  async function handleAvatarUpload(file: File | null) {
    if (!file) return;
    try {
      await uploadAvatar.mutateAsync({ agentId: agent.id, file });
      v2toast.success(t("v2.avatarUpdated"));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.avatarGenerateFailed")));
    }
  }

  async function handleGenerateAvatar() {
    try {
      await generateAvatar.mutateAsync(agent.id);
      v2toast.success(t("v2.avatarGenerated"));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.avatarGenerateFailed")));
    }
  }

  async function handleGenerateAIAvatar() {
    try {
      const result = await generateAIAvatar.mutateAsync(agent.id);
      if (result.task_id) {
        v2toast.info(t("v2.avatarAISubmitted", { taskId: result.task_id }));
      }
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.avatarGenerateFailed")));
    }
  }

  async function handleRemoveAvatar() {
    try {
      await removeAvatar.mutateAsync(agent.id);
      v2toast.success(t("v2.avatarRemoved"));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.avatarGenerateFailed")));
    }
  }

  function saveRuntimeConfig() {
    const payload: Record<string, unknown> = {
      friendly_name: friendlyName,
      description: description || null,
      provider: provider || agent.provider,
      use_provider_default: useProviderDefault,
      api_key_ref: apiKeyRef || null,
      base_url: baseUrl || null,
      system_prompt: systemPrompt || null,
      approval_mode: approvalMode,
      tool_progress_mode: toolProgressMode,
      gateway_notifications_mode: gatewayNotifMode,
      runtime_profile: runtimeProfile,
      hermes_version: hermesVersion || null,
      fallback_provider: fbProvider || null,
      fallback_model: fbModel || null,
      fallback_api_key_ref: fbKeyRef || null,
      fallback_base_url: fbBaseUrl || null,
      auxiliary_models: Object.keys(auxDraft).length > 0 ? auxDraft : null,
      runtime_type: runtimeType,
      permission_policy_id: permissionPolicyId || null,
    };
    if (!useProviderDefault) payload.model = customModel || null;
    if (runtimeType === "pi") {
      const piConfig: Record<string, unknown> = {
        tools: piTools,
        thinking_level: piThinking,
      };
      payload.pi_config = piConfig;
    }
    updateAgent
      .mutateAsync({ agentId: agent.id, payload })
      .then(() => v2toast.success(t("v2.runtimeConfigSaved")))
      .catch((error) => v2toast.error(extractErrorMessage(error, t("v2.saveFailed"))));
  }

  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20, alignItems: "start" }}>
      <section className="v2-card">
        <div className="v2-card-header"><h2 className="v2-card-title">{t("v2.identity")}</h2></div>
        <div className="v2-card-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 16, paddingBottom: 14, borderBottom: "1px solid var(--v2-border)" }}>
            <AgentAvatar agent={agent} sizeClass="h-20 w-20" />
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
              <label className="v2-btn v2-btn-ghost" style={{ cursor: isAdmin ? "pointer" : "not-allowed", fontSize: 12.5 }}>
                {t("v2.uploadAvatar")}
                <input
                  type="file"
                  accept="image/png,image/jpeg,image/webp"
                  style={{ display: "none" }}
                  onChange={(e) => void handleAvatarUpload(e.target.files?.[0] ?? null)}
                  disabled={!isAdmin}
                />
              </label>
              <button className="v2-btn v2-btn-ghost" style={{ fontSize: 12.5 }} onClick={() => void handleGenerateAvatar()} disabled={!isAdmin || generateAvatar.isPending}>
                {generateAvatar.isPending ? t("v2.generating") : t("v2.generateAvatar")}
              </button>
              <button className="v2-btn v2-btn-ghost" style={{ fontSize: 12.5 }} onClick={() => void handleGenerateAIAvatar()} disabled={!isAdmin || generateAIAvatar.isPending} title={t("v2.generateAvatarAIHint")}>
                {generateAIAvatar.isPending ? t("v2.generating") : t("v2.generateAvatarAI")}
              </button>
              <button className="v2-btn v2-btn-danger" style={{ fontSize: 12.5 }} onClick={() => void handleRemoveAvatar()} disabled={!isAdmin || !agent.has_avatar || removeAvatar.isPending}>
                {t("v2.removeAvatar")}
              </button>
            </div>
          </div>
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.friendlyName")}</label>
            <input className="v2-input" value={friendlyName} onChange={(e) => setFriendlyName(e.target.value)} disabled={!isAdmin} />
          </div>
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.slug")}</label>
            <input className="v2-input v2-mono" value={agent.slug} disabled />
          </div>
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.description")}</label>
            <input className="v2-input" value={description} onChange={(e) => setDescription(e.target.value)} disabled={!isAdmin} />
          </div>
        </div>
      </section>

      <section className="v2-card">
        <div className="v2-card-header"><h2 className="v2-card-title">{t("v2.modelProvider")}</h2></div>
        <div className="v2-card-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.provider")}</label>
            <select
              className="v2-select"
              value={provider}
              onChange={(e) => {
                const newProvider = e.target.value;
                setProvider(newProvider);
                const p = providers.find((pr) => pr.runtime_provider === newProvider);
                if (p) {
                  if (p.default_model) setCustomModel(p.default_model);
                  if (p.base_url) setBaseUrl(p.base_url);
                }
              }}
              disabled={!isAdmin}
            >
              <option value="">{agent.provider} ({t("v2.current")})</option>
              {providers.filter((p) => p.enabled).map((p) => (
                <option key={p.slug} value={p.runtime_provider}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.useProviderDefault")}</label>
            <select className="v2-select" value={useProviderDefault ? "true" : "false"} onChange={(e) => setUseProviderDefault(e.target.value === "true")} disabled={!isAdmin}>
              <option value="true">{t("v2.yes")}</option>
              <option value="false">{t("v2.noCustom")}</option>
            </select>
          </div>
          {!useProviderDefault ? (
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.model")}</label>
              {availableModels.length > 0 ? (
                <select className="v2-select" value={customModel} onChange={(e) => setCustomModel(e.target.value)} disabled={!isAdmin}>
                  {!availableModels.includes(customModel) && customModel ? <option value={customModel}>{customModel} ({t("v2.current")})</option> : null}
                  {availableModels.map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              ) : (
                <input className="v2-input" value={customModel} onChange={(e) => setCustomModel(e.target.value)} disabled={!isAdmin} />
              )}
            </div>
          ) : null}
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.apiKey")}</label>
            <select className="v2-select" value={apiKeyRef} onChange={(e) => setApiKeyRef(e.target.value)} disabled={!isAdmin}>
              <option value="">{t("v2.none")}</option>
              {secrets.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
            </select>
          </div>
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.baseUrl")}</label>
            <input className="v2-input v2-mono" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} disabled={!isAdmin}
              placeholder={agentProvider?.base_url ?? "https://api.example.com/v1"} />
          </div>
        </div>
      </section>

      <section className="v2-card">
        <div className="v2-card-header"><h2 className="v2-card-title">{t("v2.systemPrompt")}</h2></div>
        <div className="v2-card-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.systemPrompt")}</label>
            <textarea className="v2-textarea" rows={8} value={systemPrompt} onChange={(e) => setSystemPrompt(e.target.value)} disabled={!isAdmin}
              placeholder={t("v2.systemPromptPrompt")} />
          </div>
        </div>
      </section>

      <section className="v2-card">
        <div className="v2-card-header"><h2 className="v2-card-title">{t("v2.runtimeInteraction")}</h2></div>
        <div className="v2-card-body" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.runtimeProfile")}</label>
            <select className="v2-select" value={runtimeProfile} onChange={(e) => setRuntimeProfile(e.target.value)} disabled={!isAdmin}>
              <option value="standard">{t("v2.standard")}</option>
              <option value="technical">{t("v2.technical")}</option>
            </select>
          </div>
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.hermesVersion")}</label>
            <select className="v2-select" value={hermesVersion} onChange={(e) => setHermesVersion(e.target.value)} disabled={!isAdmin}>
              <option value="">{t("v2.instanceDefault")}</option>
              {hermesVersions.map((v) => <option key={v.version} value={v.release_tag ?? ""}>{v.version}</option>)}
            </select>
          </div>
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.approvalMode")}</label>
            <select className="v2-select" value={approvalMode} onChange={(e) => setApprovalMode(e.target.value)} disabled={!isAdmin}>
              <option value="inherit">{t("v2.inherit")}</option>
              <option value="off">{t("v2.off")}</option>
              <option value="on-request">{t("v2.onRequest")}</option>
              <option value="on-failure">{t("v2.onFailure")}</option>
            </select>
          </div>
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.toolProgress")}</label>
            <select className="v2-select" value={toolProgressMode} onChange={(e) => setToolProgressMode(e.target.value)} disabled={!isAdmin}>
              <option value="inherit">{t("v2.inherit")}</option>
              <option value="on">{t("v2.on")}</option>
              <option value="off">{t("v2.off")}</option>
            </select>
          </div>
          <div className="v2-field">
            <label className="v2-field-label">{t("v2.gatewayNotif")}</label>
            <select className="v2-select" value={gatewayNotifMode} onChange={(e) => setGatewayNotifMode(e.target.value)} disabled={!isAdmin}>
              <option value="inherit">{t("v2.inherit")}</option>
              <option value="all">{t("v2.allOption")}</option>
              <option value="result">{t("v2.resultOnly")}</option>
              <option value="off">{t("v2.off")}</option>
            </select>
          </div>
        </div>
      </section>

      <section className="v2-card" style={{ gridColumn: "1 / -1" }}>
        <div className="v2-card-header"><h2 className="v2-card-title">{t("v2.fallbackProvider")}</h2></div>
        <div className="v2-card-body">
          <p className="v2-field-hint" style={{ marginBottom: 14 }}>
            {t("v2.fallbackDesc")}
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr 1fr", gap: 14 }}>
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.provider")}</label>
              <select className="v2-select" value={fbProvider} onChange={(e) => setFbProvider(e.target.value)} disabled={!isAdmin}>
                <option value="">{t("v2.none")}</option>
                {providers.filter((p) => p.enabled).map((p) => <option key={p.slug} value={p.runtime_provider}>{p.name}</option>)}
              </select>
            </div>
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.model")}</label>
              {(fbProviderDef?.available_models ?? []).length > 0 ? (
                <select className="v2-select" value={fbModel} onChange={(e) => setFbModel(e.target.value)} disabled={!isAdmin || !fbProvider}>
                  <option value="">{t("v2.defaultOption")}</option>
                  {(fbProviderDef?.available_models ?? []).map((m) => <option key={m} value={m}>{m}</option>)}
                </select>
              ) : (
                <input className="v2-input" value={fbModel} onChange={(e) => setFbModel(e.target.value)} disabled={!isAdmin || !fbProvider} />
              )}
            </div>
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.apiKeyShort")}</label>
              <select className="v2-select" value={fbKeyRef} onChange={(e) => setFbKeyRef(e.target.value)} disabled={!isAdmin || !fbProvider}>
                <option value="">{t("v2.none")}</option>
                {secrets.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
              </select>
            </div>
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.baseUrl")}</label>
              <input className="v2-input v2-mono" value={fbBaseUrl} onChange={(e) => setFbBaseUrl(e.target.value)} disabled={!isAdmin || !fbProvider} />
            </div>
          </div>
        </div>
      </section>

      <section className="v2-card" style={{ gridColumn: "1 / -1" }}>
        <div className="v2-card-header"><h2 className="v2-card-title">{t("v2.auxiliaryModels")}</h2></div>
        <div className="v2-card-body">
          <p className="v2-field-hint" style={{ marginBottom: 14 }}>
            {t("v2.auxiliaryDesc")}
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 14 }}>
            {AUX_TASKS.map((task) => {
              const entry = auxDraft[task.key] ?? { provider: null, model: null, api_key_ref: null, base_url: null };
              const auxProv = providers.find((p) => p.runtime_provider === entry.provider && (p.available_models ?? []).length > 0);
              return (
                <div key={task.key} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  <div className="v2-field-label">{task.label}</div>
                  <select
                    className="v2-select"
                    value={entry.provider ?? ""}
                    onChange={(e) => setAuxDraft((prev) => ({
                      ...prev,
                      [task.key]: { ...entry, provider: e.target.value || null },
                    }))}
                    disabled={!isAdmin}
                  >
                    <option value="">{t("v2.useDefault")}</option>
                    {providers.filter((p) => p.enabled).map((p) => (
                      <option key={p.slug} value={p.runtime_provider}>{p.name}</option>
                    ))}
                  </select>
                  {entry.provider ? (
                    <>
                      <select
                        className="v2-select"
                        value={entry.model ?? ""}
                        onChange={(e) => setAuxDraft((prev) => ({
                          ...prev,
                          [task.key]: { ...entry, model: e.target.value || null },
                        }))}
                        disabled={!isAdmin}
                      >
                        <option value="">{t("v2.defaultModel")}</option>
                        {(auxProv?.available_models ?? []).map((m) => (
                          <option key={m} value={m}>{m}</option>
                        ))}
                      </select>
                      <select
                        className="v2-select"
                        value={entry.api_key_ref ?? ""}
                        onChange={(e) => setAuxDraft((prev) => ({
                          ...prev,
                          [task.key]: { ...entry, api_key_ref: e.target.value || null },
                        }))}
                        disabled={!isAdmin}
                      >
                        <option value="">{t("v2.defaultKey")}</option>
                        {secrets.map((s) => (
                          <option key={s.name} value={s.name}>{s.name}</option>
                        ))}
                      </select>
                    </>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      </section>

      <section className="v2-card" style={{ gridColumn: "1 / -1" }}>
        <div className="v2-card-header"><h2 className="v2-card-title">{t("v2.runtimeEngine")}</h2></div>
        <div className="v2-card-body">
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
            <div className="v2-field">
              <label className="v2-field-label">{t("v2.runtimeType")}</label>
              <select className="v2-select" value={runtimeType} onChange={(e) => setRuntimeType(e.target.value)} disabled={!isAdmin}>
                <option value="hermes">{t("v2.runtimeHermes")}</option>
                <option value="pi">{t("v2.runtimePi")}</option>
              </select>
            </div>
            {runtimeType === "pi" ? (
              <div className="v2-field">
                <label className="v2-field-label">{t("v2.permissionPolicy")}</label>
                <select className="v2-select" value={permissionPolicyId} onChange={(e) => setPermissionPolicyId(e.target.value)} disabled={!isAdmin}>
                  <option value="">{t("v2.none")}</option>
                  {(permissionPolicies ?? []).map((p) => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              </div>
            ) : null}
          </div>
          {runtimeType === "pi" ? (
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14, marginTop: 14 }}>
              <div className="v2-field">
                <label className="v2-field-label">{t("v2.piTools")}</label>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {["read", "bash", "edit", "write", "grep", "find", "ls"].map((tool) => (
                    <label key={tool} style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12.5 }}>
                      <input type="checkbox" checked={piTools.includes(tool)} onChange={(e) => {
                        if (e.target.checked) setPiTools([...piTools, tool]);
                        else setPiTools(piTools.filter((t) => t !== tool));
                      }} disabled={!isAdmin} />
                      {tool}
                    </label>
                  ))}
                </div>
              </div>
              <div className="v2-field">
                <label className="v2-field-label">{t("v2.piThinking")}</label>
                <select className="v2-select" value={piThinking} onChange={(e) => setPiThinking(e.target.value)} disabled={!isAdmin}>
                  <option value="off">Off</option>
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                </select>
              </div>
            </div>
          ) : null}
        </div>
      </section>

      <div style={{ gridColumn: "1 / -1", display: "flex", gap: 10, alignItems: "center" }}>
        {isAdmin ? (
          <button className="v2-btn v2-btn-primary" onClick={() => saveRuntimeConfig()} disabled={updateAgent.isPending}>
            {updateAgent.isPending ? t("v2.saving") : t("v2.saveAll")}
          </button>
        ) : null}
        <Link to={`/agents/${agent.id}`} className="v2-btn v2-btn-ghost" style={{ fontSize: 12.5 }}>
          {t("v2.openClassicView")}
        </Link>
      </div>
    </div>
  );
}

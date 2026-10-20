import { AgentMessagingPanel } from "../../components/AgentMessagingPanel";
import { useI18n } from "../../lib/i18n";
import { findMatchingProvider } from "../../lib/providers";
import type {
  Agent,
  HermesVersion,
  ProviderDefinition,
  RuntimeProfileDefinition,
  Secret,
} from "../../types/api";
import {
  approvalModeOptions,
  formatHermesVersionLabel,
  gatewayNotificationsModeOptions,
  slugify,
  thinkingModeOptions,
  toolProgressModeOptions,
} from "./utils";
import type { AgentDraftsState } from "./useAgentDrafts";

export function OverviewTab({
  agent,
  isAdmin,
  isOpen,
  onToggle,
  drafts,
  providers,
  runtimeProfiles,
  hermesVersions,
  secrets,
  updatePending,
  onSaveIdentity,
  onSaveSystemPrompt,
  onSaveRuntimeProfile,
}: {
  agent: Agent;
  isAdmin: boolean;
  isOpen: boolean;
  onToggle: () => void;
  drafts: AgentDraftsState;
  providers: ProviderDefinition[] | undefined;
  runtimeProfiles: RuntimeProfileDefinition[] | undefined;
  hermesVersions: HermesVersion[] | undefined;
  secrets: Secret[] | undefined;
  updatePending: boolean;
  onSaveIdentity: () => void;
  onSaveSystemPrompt: () => void;
  onSaveRuntimeProfile: () => void;
}) {
  const {
    identityForm,
    setIdentityForm,
    systemPromptDraft,
    setSystemPromptDraft,
    runtimeProfileDraft,
    setRuntimeProfileDraft,
    hermesVersionDraft,
    setHermesVersionDraft,
    approvalModeDraft,
    setApprovalModeDraft,
    toolProgressModeDraft,
    setToolProgressModeDraft,
    thinkingModeDraft,
    setThinkingModeDraft,
    gatewayNotificationsModeDraft,
    setGatewayNotificationsModeDraft,
    useProviderDefaultDraft,
    setUseProviderDefaultDraft,
    customModelDraft,
    setCustomModelDraft,
    fallbackDraft,
    setFallbackDraft,
    auxiliaryDraft,
    setAuxiliaryDraft,
    nameTouched,
    setNameTouched,
    slugTouched,
    setSlugTouched,
    currentRuntimeCapabilityProfile,
    selectedRuntimeProfile,
    effectiveHermesVersionEntry,
    selectedHermesVersionEntry,
  } = drafts;
  const { t } = useI18n();
  return (
    <section className="agent-config panel-frame p-6">
      <button
        type="button"
        className="agent-section-toggle flex w-full items-end justify-between gap-4 border-b border-[var(--border)] pb-4 text-left"
        onClick={onToggle}
      >
        <div>
          <p className="panel-label">{t("agent.configuration")}</p>
          <h3 className="mt-2 text-2xl text-[var(--text-display)]">{t("agent.runtimeSettings")}</h3>
        </div>
        <div className="text-right">
          <p className="panel-label">{agent.provider} / {agent.use_provider_default ? "provider default" : agent.model}</p>
          <p className="mt-2 font-mono text-xs uppercase tracking-[0.1em] text-[var(--text-secondary)]">
            {isOpen ? "Collapse" : "Expand"}
          </p>
        </div>
      </button>
      {isOpen ? (
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
                disabled={updatePending}
                onClick={onSaveIdentity}
              >
                {updatePending ? t("common.loading") : t("agent.saveIdentity")}
              </button>
              <p className="panel-inline-status">{t("agent.identityHint")}</p>
            </div>
          </div>
          <div className="mt-6 space-y-6">
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
                  disabled={updatePending}
                  onClick={onSaveSystemPrompt}
                >
                  {updatePending ? t("common.loading") : t("agent.saveSystemPrompt")}
                </button>
                <p className="panel-inline-status">{t("agent.systemPromptHint")}</p>
              </div>
            </div>
            <div className="space-y-4">
              <section className="rounded-[1.25rem] border border-[var(--border)] bg-[color-mix(in_srgb,var(--surface)_92%,transparent)] p-5">
                <div className="flex flex-col gap-3 border-b border-[var(--border)] pb-4 lg:flex-row lg:items-end lg:justify-between">
                  <div>
                    <p className="panel-label">{t("agent.runtimeSnapshot")}</p>
                    <h4 className="mt-2 text-lg text-[var(--text-display)]">{t("agent.runtimeSummary")}</h4>
                  </div>
                  <p className="max-w-2xl text-sm leading-6 text-[var(--text-secondary)] lg:text-right">
                    {t("agent.runtimeSnapshotCopy")}
                  </p>
                </div>
                <div className="mt-4 grid gap-3 sm:grid-cols-2 2xl:grid-cols-3">
                  {[
                    { label: t("agents.provider"), value: (() => {
                      const matched = findMatchingProvider(providers, agent.provider, agent.base_url);
                      return matched ? matched.name : agent.provider;
                    })() },
                    { label: t("agents.model"), value: agent.use_provider_default ? `${agent.model} (provider default)` : agent.model },
                    { label: t("agents.runtimeProfile"), value: currentRuntimeCapabilityProfile?.name ?? agent.runtime_profile },
                    {
                      label: t("agent.effectiveHermesVersion"),
                      value: formatHermesVersionLabel(
                        effectiveHermesVersionEntry?.version ?? agent.hermes_version,
                        effectiveHermesVersionEntry?.detected_version ?? null,
                      ),
                    },
                    { label: t("agents.secretRef"), value: agent.api_key_ref ?? t("agent.none") },
                    { label: t("agent.fallbackProvider"), value: (() => {
                      if (!agent.fallback_provider) return t("agent.none");
                      const fb = findMatchingProvider(providers, agent.fallback_provider, null);
                      const fbName = fb ? fb.name : agent.fallback_provider;
                      return `${fbName} / ${agent.fallback_model ?? "—"}`;
                    })() },
                    { label: t("agents.node"), value: agent.node?.name ?? t("agent.localRuntime") },
                  ].map((item) => (
                    <div
                      key={item.label}
                      className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] px-4 py-3"
                    >
                      <p className="panel-label">{item.label}</p>
                      <p className="mt-2 break-words text-sm leading-6 text-[var(--text-display)]">{item.value}</p>
                    </div>
                  ))}
                  <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] px-4 py-3 sm:col-span-2 2xl:col-span-3">
                    <p className="panel-label">{t("agent.workspacePath")}</p>
                    <p className="mt-2 break-all text-sm leading-6 text-[var(--text-display)]">{agent.workspace_path}</p>
                  </div>
                </div>
              </section>

              <section className="rounded-[1.25rem] border border-[var(--border)] bg-[color-mix(in_srgb,var(--surface)_92%,transparent)] p-5">
                <div className="flex flex-col gap-3 border-b border-[var(--border)] pb-4 lg:flex-row lg:items-end lg:justify-between">
                  <div>
                    <p className="panel-label">{t("agent.runtimeControls")}</p>
                    <h4 className="mt-2 text-lg text-[var(--text-display)]">{t("agent.runtimeSettings")}</h4>
                  </div>
                  <p className="max-w-2xl text-sm leading-6 text-[var(--text-secondary)] lg:text-right">
                    {t("agent.runtimeControlsCopy")}
                  </p>
                </div>

                <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,0.95fr)]">
                  <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-4">
                    <div>
                      <p className="panel-label">{t("agents.runtimeProfile")}</p>
                      <h5 className="mt-2 text-base text-[var(--text-display)]">
                        {selectedRuntimeProfile?.name ?? agent.runtime_profile}
                      </h5>
                    </div>
                    <div className="mt-4">
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
                          <div className="rounded-2xl border border-[var(--border-visible)] bg-[color-mix(in_srgb,var(--surface)_86%,transparent)] px-4 py-3 text-sm text-[var(--text-display)]">
                            {selectedRuntimeProfile?.name ?? agent.runtime_profile}
                          </div>
                        )}
                      </label>
                    </div>
                    {selectedRuntimeProfile ? (
                      <div className="mt-4 space-y-3 text-sm leading-6 text-[var(--text-secondary)]">
                        <p>{selectedRuntimeProfile.description}</p>
                        <p>{selectedRuntimeProfile.tooling_summary}</p>
                        <p className="rounded-xl border border-[var(--border)] bg-[color-mix(in_srgb,var(--surface)_84%,transparent)] px-3 py-2 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                          {t("agents.profileFutureImage", { value: selectedRuntimeProfile.container_intent })}
                        </p>
                      </div>
                    ) : null}
                  </div>

                  <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-4">
                    <div>
                      <p className="panel-label">{t("agent.effectiveHermesVersion")}</p>
                      <h5 className="mt-2 text-base text-[var(--text-display)]">
                        {formatHermesVersionLabel(
                          selectedHermesVersionEntry?.version ?? effectiveHermesVersionEntry?.version ?? agent.hermes_version,
                          selectedHermesVersionEntry?.detected_version ?? effectiveHermesVersionEntry?.detected_version ?? null,
                        )}
                      </h5>
                    </div>
                    <div className="mt-4">
                      <label className="panel-field">
                        <span className="panel-label">Hermes Agent</span>
                        {isAdmin ? (
                          <select
                            value={hermesVersionDraft}
                            onChange={(event) => setHermesVersionDraft(event.target.value)}
                          >
                            <option value="bundled">Inherit instance default / bundled</option>
                            {(hermesVersions ?? [])
                              .filter((item) => item.version !== "bundled" && item.installed)
                              .map((item) => (
                                <option key={item.version} value={item.version}>
                                  {formatHermesVersionLabel(item.version, item.detected_version)}
                                </option>
                              ))}
                          </select>
                        ) : (
                          <div className="rounded-2xl border border-[var(--border-visible)] bg-[color-mix(in_srgb,var(--surface)_86%,transparent)] px-4 py-3 text-sm text-[var(--text-display)]">
                            {formatHermesVersionLabel(
                              effectiveHermesVersionEntry?.version ?? agent.hermes_version,
                              effectiveHermesVersionEntry?.detected_version ?? null,
                            )}
                          </div>
                        )}
                      </label>
                    </div>
                    <div className="mt-4 space-y-3 text-sm leading-6 text-[var(--text-secondary)]">
                      <p>{t("agent.runtimeVersionHint")}</p>
                      <p className="rounded-xl border border-[var(--border)] bg-[color-mix(in_srgb,var(--surface)_84%,transparent)] px-3 py-2">
                        {hermesVersionDraft === "bundled"
                          ? t("agent.runtimeVersionInherited", {
                            value: formatHermesVersionLabel(
                              selectedHermesVersionEntry?.version ?? "bundled",
                              selectedHermesVersionEntry?.detected_version ?? null,
                            ),
                          })
                          : t("agent.runtimeVersionPinned")}
                      </p>
                      {selectedHermesVersionEntry?.detected_version_warning ? (
                        <p className="rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-100">
                          {selectedHermesVersionEntry.detected_version_warning}
                        </p>
                      ) : null}
                    </div>
                  </div>
                </div>

                <div className="mt-4 rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-4">
                  <div className="border-b border-[var(--border)] pb-4">
                    <p className="panel-label">{t("agent.interactionSettings")}</p>
                    <h5 className="mt-2 text-base text-[var(--text-display)]">{t("agent.advancedRuntimeBehavior")}</h5>
                    <p className="mt-3 max-w-3xl text-sm leading-6 text-[var(--text-secondary)]">
                      {t("agent.advancedRuntimeBehaviorCopy")}
                    </p>
                  </div>
                  <div className="mt-4 grid gap-4 lg:grid-cols-3">
                    <label className="panel-field">
                      <span className="panel-label">{t("agent.approvalMode")}</span>
                      {isAdmin ? (
                        <select
                          value={approvalModeDraft}
                          onChange={(event) => setApprovalModeDraft(event.target.value)}
                        >
                          {approvalModeOptions.map((option) => (
                            <option key={option.value} value={option.value}>
                              {t(option.labelKey)}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <div className="rounded-2xl border border-[var(--border-visible)] bg-[color-mix(in_srgb,var(--surface)_86%,transparent)] px-4 py-3 text-sm text-[var(--text-display)]">
                          {t(approvalModeOptions.find((option) => option.value === approvalModeDraft)?.labelKey ?? "agent.interactionModeInherit")}
                        </div>
                      )}
                    </label>

                    <label className="panel-field">
                      <span className="panel-label">{t("agent.toolProgressMode")}</span>
                      {isAdmin ? (
                        <select
                          value={toolProgressModeDraft}
                          onChange={(event) => setToolProgressModeDraft(event.target.value)}
                        >
                          {toolProgressModeOptions.map((option) => (
                            <option key={option.value} value={option.value}>
                              {t(option.labelKey)}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <div className="rounded-2xl border border-[var(--border-visible)] bg-[color-mix(in_srgb,var(--surface)_86%,transparent)] px-4 py-3 text-sm text-[var(--text-display)]">
                          {t(toolProgressModeOptions.find((option) => option.value === toolProgressModeDraft)?.labelKey ?? "agent.interactionModeInherit")}
                        </div>
                      )}
                    </label>

                    <label className="panel-field">
                      <span className="panel-label">{t("agent.thinkingMode")}</span>
                      {isAdmin ? (
                        <select
                          value={thinkingModeDraft}
                          onChange={(event) => setThinkingModeDraft(event.target.value)}
                        >
                          {thinkingModeOptions.map((option) => (
                            <option key={option.value} value={option.value}>
                              {t(option.labelKey)}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <div className="rounded-2xl border border-[var(--border-visible)] bg-[color-mix(in_srgb,var(--surface)_86%,transparent)] px-4 py-3 text-sm text-[var(--text-display)]">
                          {t(thinkingModeOptions.find((option) => option.value === thinkingModeDraft)?.labelKey ?? "agent.interactionModeInherit")}
                        </div>
                      )}
                      <span className="text-xs text-[var(--text-secondary)]">{t("agent.thinkingModeHint")}</span>
                    </label>

                    <label className="panel-field">
                      <span className="panel-label">{t("agent.gatewayNotificationsMode")}</span>
                      {isAdmin ? (
                        <select
                          value={gatewayNotificationsModeDraft}
                          onChange={(event) => setGatewayNotificationsModeDraft(event.target.value)}
                        >
                          {gatewayNotificationsModeOptions.map((option) => (
                            <option key={option.value} value={option.value}>
                              {t(option.labelKey)}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <div className="rounded-2xl border border-[var(--border-visible)] bg-[color-mix(in_srgb,var(--surface)_86%,transparent)] px-4 py-3 text-sm text-[var(--text-display)]">
                          {t(gatewayNotificationsModeOptions.find((option) => option.value === gatewayNotificationsModeDraft)?.labelKey ?? "agent.interactionModeInherit")}
                        </div>
                      )}
                    </label>
                  </div>
                </div>

                <div className="mt-4 rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-4">
                  <div className="border-b border-[var(--border)] pb-4">
                    <p className="panel-label">{t("agent.modelSource")}</p>
                    <h5 className="mt-2 text-base text-[var(--text-display)]">{t("agent.modelSourceDesc")}</h5>
                  </div>
                  <div className="mt-4">
                    <label className="panel-field">
                      <span className="panel-label">{t("agent.useProviderDefault")}</span>
                      {isAdmin ? (
                        <select
                          value={useProviderDefaultDraft ? "true" : "false"}
                          onChange={(event) => setUseProviderDefaultDraft(event.target.value === "true")}
                        >
                          <option value="true">{t("agent.useProviderDefaultYes")}</option>
                          <option value="false">{t("agent.useProviderDefaultNo")}</option>
                        </select>
                      ) : (
                        <div className="rounded-2xl border border-[var(--border-visible)] bg-[color-mix(in_srgb,var(--surface)_86%,transparent)] px-4 py-3 text-sm text-[var(--text-display)]">
                          {useProviderDefaultDraft ? t("agent.useProviderDefaultYes") : t("agent.useProviderDefaultNo")}
                        </div>
                      )}
                    </label>
                    {!useProviderDefaultDraft && (
                      <label className="panel-field mt-4">
                        <span className="panel-label">{t("agents.model")}</span>
                        {isAdmin ? (
                          (() => {
                            const agentProvider = providers?.find(
                              (p) => p.runtime_provider === agent?.provider && (p.available_models ?? []).length > 0,
                            ) ?? providers?.find((p) => p.slug === agent?.provider);
                            const providerModels = [
                              ...(agentProvider?.default_model ? [agentProvider.default_model] : []),
                              ...(agentProvider?.available_models ?? []),
                            ].filter((v, i, a) => a.indexOf(v) === i);
                            if (providerModels.length > 0) {
                              return (
                                <select
                                  value={customModelDraft}
                                  onChange={(event) => setCustomModelDraft(event.target.value)}
                                >
                                  {providerModels.map((m) => (
                                    <option key={m} value={m}>{m}</option>
                                  ))}
                                </select>
                              );
                            }
                            return (
                              <input
                                type="text"
                                value={customModelDraft}
                                placeholder="anthropic/claude-sonnet-4"
                                onChange={(event) => setCustomModelDraft(event.target.value)}
                              />
                            );
                          })()
                        ) : (
                          <div className="rounded-2xl border border-[var(--border-visible)] bg-[color-mix(in_srgb,var(--surface)_86%,transparent)] px-4 py-3 text-sm text-[var(--text-display)]">
                            {customModelDraft}
                          </div>
                        )}
                      </label>
                    )}
                  </div>
                </div>

                <div className="mt-4 rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-4">
                  <div className="border-b border-[var(--border)] pb-4">
                    <p className="panel-label">{t("agent.fallbackSectionTitle")}</p>
                    <h5 className="mt-2 text-base text-[var(--text-display)]">{t("agent.fallbackSectionDesc")}</h5>
                    <p className="mt-3 max-w-3xl text-sm leading-6 text-[var(--text-secondary)]">
                      {t("agent.fallbackHint")}
                    </p>
                  </div>
                  <div className="mt-4 grid gap-4 lg:grid-cols-2">
                    <label className="panel-field">
                      <span className="panel-label">{t("agent.fallbackProvider")}</span>
                      {isAdmin ? (
                        <input
                          type="text"
                          value={fallbackDraft.provider ?? ""}
                          placeholder={agent.provider || "openrouter"}
                          onChange={(e) => setFallbackDraft((d) => ({ ...d, provider: e.target.value || null }))}
                        />
                      ) : (
                        <div className="rounded-2xl border border-[var(--border-visible)] bg-[color-mix(in_srgb,var(--surface)_86%,transparent)] px-4 py-3 text-sm text-[var(--text-display)]">
                          {agent.fallback_provider || "—"}
                        </div>
                      )}
                    </label>
                    <label className="panel-field">
                      <span className="panel-label">{t("agent.fallbackModel")}</span>
                      {isAdmin ? (
                        <input
                          type="text"
                          value={fallbackDraft.model ?? ""}
                          placeholder={agent.model || "anthropic/claude-sonnet-4"}
                          onChange={(e) => setFallbackDraft((d) => ({ ...d, model: e.target.value || null }))}
                        />
                      ) : (
                        <div className="rounded-2xl border border-[var(--border-visible)] bg-[color-mix(in_srgb,var(--surface)_86%,transparent)] px-4 py-3 text-sm text-[var(--text-display)]">
                          {agent.fallback_model || "—"}
                        </div>
                      )}
                    </label>
                    <label className="panel-field">
                      <span className="panel-label">{t("agent.fallbackApiKey")}</span>
                      {isAdmin ? (
                        <select
                          value={fallbackDraft.api_key_ref ?? ""}
                          onChange={(e) => setFallbackDraft((d) => ({ ...d, api_key_ref: e.target.value || null }))}
                        >
                          <option value="">{t("agent.none")}</option>
                          {(secrets ?? []).map((s) => (
                            <option key={s.id} value={s.name}>
                              {s.name}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <div className="rounded-2xl border border-[var(--border-visible)] bg-[color-mix(in_srgb,var(--surface)_86%,transparent)] px-4 py-3 text-sm text-[var(--text-display)]">
                          {agent.fallback_api_key_ref || "—"}
                        </div>
                      )}
                    </label>
                    <label className="panel-field">
                      <span className="panel-label">{t("agent.fallbackBaseUrl")}</span>
                      {isAdmin ? (
                        <input
                          type="text"
                          value={fallbackDraft.base_url ?? ""}
                          placeholder="https://..."
                          onChange={(e) => setFallbackDraft((d) => ({ ...d, base_url: e.target.value || null }))}
                        />
                      ) : (
                        <div className="rounded-2xl border border-[var(--border-visible)] bg-[color-mix(in_srgb,var(--surface)_86%,transparent)] px-4 py-3 text-sm text-[var(--text-display)]">
                          {agent.fallback_base_url || "—"}
                        </div>
                      )}
                    </label>
                  </div>
                </div>

                <div className="mt-4 rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-4">
                  <div className="border-b border-[var(--border)] pb-4">
                    <p className="panel-label">{t("agent.auxiliarySectionTitle")}</p>
                    <h5 className="mt-2 text-base text-[var(--text-display)]">{t("agent.auxiliarySectionDesc")}</h5>
                  </div>
                  <div className="mt-4 space-y-4">
                    {(["vision", "compression", "web_extract", "approval"] as const).map((task) => (
                      <div key={task} className="grid gap-4 rounded-2xl border border-[var(--border)] bg-[var(--surface-alt)] p-3 lg:grid-cols-4">
                        <label className="panel-field">
                          <span className="panel-label">{t(`agent.auxiliaryTask.${task}`)}</span>
                          {isAdmin ? (
                            (() => {
                              const auxEntry = auxiliaryDraft[task] || { provider: null, model: null, api_key_ref: null, base_url: null };
                              const selectedAuxProvider = providers?.find((p) => p.slug === auxEntry.provider);
                              const models = selectedAuxProvider?.available_models;
                              return (
                                <>
                                  <div className="grid gap-2">
                                    <select
                                      value={auxEntry.provider ?? ""}
                                      onChange={(e) => setAuxiliaryDraft((d) => ({
                                        ...d,
                                        [task]: { ...((d[task] || {})), provider: e.target.value || null, model: null },
                                      }))}
                                    >
                                      <option value="">{t("agent.none")}</option>
                                      {providers?.map((p) => (
                                        <option key={p.slug} value={p.slug}>{p.name}</option>
                                      ))}
                                    </select>
                                    {auxEntry.provider && (
                                      models && models.length > 0 ? (
                                        <select
                                          value={auxEntry.model ?? ""}
                                          onChange={(e) => setAuxiliaryDraft((d) => ({
                                            ...d,
                                            [task]: { ...((d[task] || {})), model: e.target.value || null },
                                          }))}
                                        >
                                          <option value="">{t("agent.auxiliaryDefaultModel")}</option>
                                          {models.map((m) => (
                                            <option key={m} value={m}>{m}</option>
                                          ))}
                                        </select>
                                      ) : (
                                        <input
                                          value={auxEntry.model ?? ""}
                                          placeholder={t("agent.auxiliaryDefaultModel")}
                                          onChange={(e) => setAuxiliaryDraft((d) => ({
                                            ...d,
                                            [task]: { ...((d[task] || {})), model: e.target.value || null },
                                          }))}
                                        />
                                      )
                                    )}
                                  </div>
                                </>
                              );
                            })()
                          ) : (
                            <div className="rounded-2xl border border-[var(--border-visible)] bg-[color-mix(in_srgb,var(--surface)_86%,transparent)] px-4 py-3 text-sm text-[var(--text-display)]">
                              {auxiliaryDraft[task]?.provider
                                ? `${auxiliaryDraft[task].provider} / ${auxiliaryDraft[task].model || "default"}`
                                : t("agent.none")}
                            </div>
                          )}
                        </label>
                      </div>
                    ))}
                  </div>
                </div>

                {isAdmin ? (
                  <div className="mt-5 flex flex-col gap-3 rounded-2xl border border-[var(--border)] bg-[var(--surface)] p-4 lg:flex-row lg:items-center lg:justify-between">
                    <p className="max-w-2xl text-sm leading-6 text-[var(--text-secondary)]">{t("agent.runtimeProfileHint")}</p>
                    <button
                      type="button"
                      className="panel-button-secondary"
                      disabled={updatePending}
                      onClick={onSaveRuntimeProfile}
                    >
                      {updatePending ? t("common.loading") : t("agent.saveRuntimeSettings")}
                    </button>
                  </div>
                ) : null}
              </section>
            </div>

            <AgentMessagingPanel agentId={agent.id} isAdmin={isAdmin} />
          </div>
        </div>
      ) : null}
    </section>
  );
}

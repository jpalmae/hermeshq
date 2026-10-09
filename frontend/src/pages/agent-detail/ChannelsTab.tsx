import type { Dispatch, SetStateAction } from "react";

import { useI18n } from "../../lib/i18n";
import type {
  Agent,
  ManagedIntegrationDefinition,
  RuntimeCapabilityOverview,
  RuntimeProfileCapabilityDefinition,
  Secret,
} from "../../types/api";
import { SectionShell } from "./SectionShell";
import type { IntegrationResult, SectionKey, SectionState } from "./utils";

export function ChannelsTab({
  agent,
  isAdmin,
  managedIntegrations,
  integrationDrafts,
  setIntegrationDrafts,
  integrationTestResults,
  integrationActionResults,
  integrationPending,
  secretsByProvider,
  runtimeCapabilityOverview,
  currentRuntimeCapabilityProfile,
  enabledManagedIntegrations,
  updatePending,
  onSaveIntegration,
  onDisableIntegration,
  onTestIntegration,
  onRunIntegrationAction,
  sectionState,
  onToggleSection,
}: {
  agent: Agent;
  isAdmin: boolean;
  managedIntegrations: ManagedIntegrationDefinition[] | undefined;
  integrationDrafts: Record<string, Record<string, string>>;
  setIntegrationDrafts: Dispatch<SetStateAction<Record<string, Record<string, string>>>>;
  integrationTestResults: Record<string, IntegrationResult>;
  integrationActionResults: Record<string, Record<string, IntegrationResult>>;
  integrationPending: Record<string, string | null>;
  secretsByProvider: Map<string, Secret[]>;
  runtimeCapabilityOverview: RuntimeCapabilityOverview | undefined;
  currentRuntimeCapabilityProfile: RuntimeProfileCapabilityDefinition | null;
  enabledManagedIntegrations: ManagedIntegrationDefinition[];
  updatePending: boolean;
  onSaveIntegration: (integrationSlug: string) => void;
  onDisableIntegration: (integrationSlug: string) => void;
  onTestIntegration: (integrationSlug: string) => void;
  onRunIntegrationAction: (integrationSlug: string, actionSlug: string) => void;
  sectionState: SectionState;
  onToggleSection: (section: SectionKey) => void;
}) {
  const { t } = useI18n();
  return (
    <SectionShell
      eyebrow={t("agent.integrations")}
      title={t("agent.integrationRegistry")}
      meta={t("agent.availableCount", { count: managedIntegrations?.length ?? 0 })}
      isOpen={sectionState.integrations}
      onToggle={() => onToggleSection("integrations")}
    >
      <div className="space-y-5">
        <article className="agent-integration-card border border-[var(--border)] bg-[var(--surface-raised)] p-5">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <p className="panel-label">{t("agent.effectiveCapabilities")}</p>
              <h4 className="mt-2 text-xl text-[var(--text-display)]">
                {currentRuntimeCapabilityProfile?.name ?? agent.runtime_profile}
              </h4>
            </div>
            <span className="agent-capability-badge rounded-full border border-[var(--border)] px-3 py-1 text-xs text-[var(--text-secondary)]">
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
                        className="agent-capability-badge rounded-full border border-[var(--border)] px-3 py-1 font-mono text-xs text-[var(--text-secondary)]"
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
                              className="agent-capability-badge rounded-full border border-[var(--border)] px-3 py-1 font-mono text-xs text-[var(--text-secondary)]"
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
            const enabled = Boolean(agent.integration_configs?.[integration.slug]);
            const draft = integrationDrafts[integration.slug] ?? {};
            const testResult = integrationTestResults[integration.slug];
            const actionResults = integrationActionResults[integration.slug] ?? {};
            return (
              <article key={integration.slug} className="agent-integration-card border border-[var(--border)] bg-[var(--surface-raised)] p-5">
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
                    const fieldValue = draft[field.name] ?? "";
                    const updateFieldValue = (value: string) =>
                      setIntegrationDrafts((current) => ({
                        ...current,
                        [integration.slug]: {
                          ...(current[integration.slug] ?? {}),
                          [field.name]: value,
                        },
                      }));
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
                              value={fieldValue}
                              onChange={(event) => updateFieldValue(event.target.value)}
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
                        ) : field.kind === "select" ? (
                          <select
                            value={fieldValue || integration.defaults[field.name] || ""}
                            onChange={(event) => updateFieldValue(event.target.value)}
                            disabled={!isAdmin}
                          >
                            <option value="">{field.placeholder ?? t("agent.none")}</option>
                            {field.options.map((option) => (
                              <option key={option} value={option}>
                                {option}
                              </option>
                            ))}
                          </select>
                        ) : field.kind === "boolean" ? (
                          <select
                            value={fieldValue || integration.defaults[field.name] || "false"}
                            onChange={(event) => updateFieldValue(event.target.value)}
                            disabled={!isAdmin}
                          >
                            <option value="true">{t("common.yes")}</option>
                            <option value="false">{t("common.no")}</option>
                          </select>
                        ) : (
                          <input
                            value={fieldValue}
                            onChange={(event) => updateFieldValue(event.target.value)}
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
                      disabled={updatePending || integrationPending[integration.slug] === "save"}
                      onClick={() => void onSaveIntegration(integration.slug)}
                    >
                      {integrationPending[integration.slug] === "save" ? t("common.loading") : enabled ? t("agent.saveIntegration") : t("agent.enableIntegration")}
                    </button>
                    <button
                      type="button"
                      className="panel-button-secondary"
                      disabled={!enabled || integrationPending[integration.slug] === "test"}
                      onClick={() => void onTestIntegration(integration.slug)}
                    >
                      {integrationPending[integration.slug] === "test" ? t("common.loading") : t("agent.testIntegration")}
                    </button>
                    {enabled ? (
                      <button
                        type="button"
                        className="panel-button-secondary"
                        disabled={updatePending || integrationPending[integration.slug] === "disable"}
                        onClick={() => void onDisableIntegration(integration.slug)}
                      >
                        {t("agent.disableIntegration")}
                      </button>
                    ) : null}
                  </div>
                ) : null}
                {enabled && integration.actions.length ? (
                  <div className="mt-4 border-t border-[var(--border)] pt-4">
                    <p className="panel-label">{t("agent.integrationActions")}</p>
                    <div className="mt-3 flex flex-wrap gap-3">
                      {integration.actions.map((action) => (
                        <button
                          key={action.slug}
                          type="button"
                          className="panel-button-secondary"
                          disabled={integrationPending[integration.slug] === action.slug}
                          onClick={() => void onRunIntegrationAction(integration.slug, action.slug)}
                          title={action.description ?? undefined}
                        >
                          {integrationPending[integration.slug] === action.slug ? t("common.loading") : action.label}
                        </button>
                      ))}
                    </div>
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
                {Object.entries(actionResults).map(([actionSlug, result]) => (
                  <div key={actionSlug} className="mt-4 border-t border-[var(--border)] pt-4 text-sm">
                    <p className="panel-label">
                      {t("agent.integrationActionResult", { value: actionSlug })}
                    </p>
                    <p className={`mt-2 ${result.success ? "text-[var(--success)]" : "text-[var(--danger)]"}`}>
                      {result.message}
                    </p>
                    {result.details ? (
                      <pre className="mt-3 overflow-x-auto whitespace-pre-wrap text-xs leading-6 text-[var(--text-secondary)]">
                        {JSON.stringify(result.details, null, 2)}
                      </pre>
                    ) : null}
                  </div>
                ))}
              </article>
            );
          })
        ) : (
          <p className="panel-inline-status">{t("agent.emptyIntegrations")}</p>
        )}
      </div>
    </SectionShell>
  );
}

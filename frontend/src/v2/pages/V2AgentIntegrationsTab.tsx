import { useState } from "react";

import { useSecrets } from "../../api/secrets";
import { useManagedIntegrations } from "../../api/managedIntegrations";
import { useTestAgentIntegration, useRunAgentIntegrationAction, useUpdateAgent } from "../../api/agents";
import { v2toast, extractErrorMessage } from "../toast";
import type { Agent, ManagedIntegrationDefinition } from "../../types/api";

export function V2AgentIntegrationsTab({ agent, isAdmin }: { agent: Agent; isAdmin: boolean }) {
  const { data: managedIntegrations } = useManagedIntegrations();
  const { data: secrets } = useSecrets(isAdmin);
  const updateAgent = useUpdateAgent();
  const testIntegration = useTestAgentIntegration();
  const runAction = useRunAgentIntegrationAction();

  const [drafts, setDrafts] = useState<Record<string, Record<string, string>>>({});
  const [testResults, setTestResults] = useState<Record<string, { success: boolean; message: string } | null>>({});

  const configs = agent.integration_configs ?? {};

  function getDraft(integration: ManagedIntegrationDefinition, fieldName: string, defaultValue = ""): string {
    return drafts[integration.slug]?.[fieldName] ?? (configs[integration.slug]?.[fieldName] as string) ?? integration.defaults?.[fieldName] ?? defaultValue;
  }

  function setDraft(slug: string, fieldName: string, value: string) {
    setDrafts((prev) => ({
      ...prev,
      [slug]: { ...(prev[slug] ?? {}), [fieldName]: value },
    }));
  }

  async function onSaveIntegration(integration: ManagedIntegrationDefinition) {
    const current = configs[integration.slug] ?? {};
    const draft = drafts[integration.slug] ?? {};
    const merged = { ...current, ...draft };
    const newConfigs = { ...configs, [integration.slug]: merged };
    const skills = [...new Set([...(agent.skills ?? []), integration.skill_identifier].filter(Boolean))];
    try {
      await updateAgent.mutateAsync({
        agentId: agent.id,
        payload: { integration_configs: newConfigs, skills },
      });
      setDrafts((prev) => ({ ...prev, [integration.slug]: {} }));
      v2toast.success(`${integration.name} enabled`);
    } catch (error) {
      v2toast.error(extractErrorMessage(error, "Save failed"));
    }
  }

  async function onDisableIntegration(integration: ManagedIntegrationDefinition) {
    const newConfigs = { ...configs };
    delete newConfigs[integration.slug];
    const skills = (agent.skills ?? []).filter((s) => s !== integration.skill_identifier);
    try {
      await updateAgent.mutateAsync({
        agentId: agent.id,
        payload: { integration_configs: newConfigs, skills },
      });
      v2toast.success(`${integration.name} disabled`);
    } catch (error) {
      v2toast.error(extractErrorMessage(error, "Disable failed"));
    }
  }

  async function onTestIntegration(integration: ManagedIntegrationDefinition) {
    try {
      const result = await testIntegration.mutateAsync({
        agentId: agent.id,
        integrationSlug: integration.slug,
      });
      setTestResults((prev) => ({ ...prev, [integration.slug]: result }));
      if (result.success) {
        v2toast.success(`${integration.name}: connection OK`);
      } else {
        v2toast.error(`${integration.name}: ${result.message}`);
      }
    } catch (error) {
      setTestResults((prev) => ({ ...prev, [integration.slug]: { success: false, message: extractErrorMessage(error) } }));
      v2toast.error(`${integration.name}: test failed`);
    }
  }

  async function onRunAction(integration: ManagedIntegrationDefinition, actionSlug: string) {
    try {
      const result = await runAction.mutateAsync({
        agentId: agent.id,
        integrationSlug: integration.slug,
        actionSlug,
        arguments: {},
      });
      if (result.success) {
        v2toast.success(`${integration.name}: ${result.message}`);
      } else {
        v2toast.error(`${integration.name}: ${result.message}`);
      }
    } catch (error) {
      v2toast.error(extractErrorMessage(error, "Action failed"));
    }
  }

  const enabledSlugs = Object.keys(configs);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {(managedIntegrations ?? []).map((integration) => {
        const enabled = enabledSlugs.includes(integration.slug);
        const testResult = testResults[integration.slug];
        return (
          <section key={integration.slug} className="v2-card">
            <div className="v2-card-header">
              <div>
                <h2 className="v2-card-title">{integration.name}</h2>
                <p style={{ fontSize: 12, color: "var(--v2-text-muted)", marginTop: 2 }}>{integration.description}</p>
              </div>
              <span className="v2-pill" data-tone={enabled ? "success" : "neutral"}>
                <span className="v2-pill-dot" />
                {enabled ? "enabled" : "disabled"}
              </span>
            </div>
            <div className="v2-card-body" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {integration.fields.map((field) => {
                const isSecret = field.kind === "secret";
                const value = getDraft(integration, field.name);
                return (
                  <div key={field.name} className="v2-field">
                    <label className="v2-field-label">{field.label}</label>
                    {isSecret && secrets?.length ? (
                      <select
                        className="v2-select"
                        value={value}
                        onChange={(e) => setDraft(integration.slug, field.name, e.target.value)}
                        disabled={!isAdmin}
                      >
                        <option value="">Select secret…</option>
                        {secrets.map((s) => (
                          <option key={s.name} value={s.name}>{s.name}</option>
                        ))}
                      </select>
                    ) : field.options?.length ? (
                      <select
                        className="v2-select"
                        value={value}
                        onChange={(e) => setDraft(integration.slug, field.name, e.target.value)}
                        disabled={!isAdmin}
                      >
                        <option value="">Select…</option>
                        {field.options.map((opt) => (
                          <option key={opt} value={opt}>{opt}</option>
                        ))}
                      </select>
                    ) : (
                      <input
                        className="v2-input"
                        type={isSecret ? "password" : "text"}
                        value={value}
                        placeholder={field.placeholder ?? ""}
                        onChange={(e) => setDraft(integration.slug, field.name, e.target.value)}
                        disabled={!isAdmin}
                      />
                    )}
                  </div>
                );
              })}

              {testResult ? (
                <p style={{ fontSize: 12.5, color: testResult.success ? "var(--v2-success)" : "var(--v2-danger)" }}>
                  {testResult.success ? "✓ " : "✗ "}{testResult.message}
                </p>
              ) : null}

              {isAdmin ? (
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <button
                    className="v2-btn v2-btn-primary"
                    style={{ padding: "6px 12px", fontSize: 12.5 }}
                    onClick={() => void onSaveIntegration(integration)}
                    disabled={updateAgent.isPending}
                  >
                    {enabled ? "Save" : "Enable"}
                  </button>
                  <button
                    className="v2-btn v2-btn-secondary"
                    style={{ padding: "6px 12px", fontSize: 12.5 }}
                    onClick={() => void onTestIntegration(integration)}
                    disabled={!enabled || testIntegration.isPending}
                  >
                    {testIntegration.isPending ? "Testing…" : "Test connection"}
                  </button>
                  {enabled ? (
                    <button
                      className="v2-btn v2-btn-danger"
                      style={{ padding: "6px 12px", fontSize: 12.5 }}
                      onClick={() => void onDisableIntegration(integration)}
                      disabled={updateAgent.isPending}
                    >
                      Disable
                    </button>
                  ) : null}
                  {enabled && integration.actions?.map((action) => (
                    <button
                      key={action.slug}
                      className="v2-btn v2-btn-secondary"
                      style={{ padding: "6px 12px", fontSize: 12.5 }}
                      onClick={() => void onRunAction(integration, action.slug)}
                      disabled={runAction.isPending}
                    >
                      {action.label}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          </section>
        );
      })}
      {(managedIntegrations ?? []).length === 0 ? (
        <div className="v2-empty">
          <p className="v2-empty-title">No integrations available</p>
          <p className="v2-empty-text">Managed integrations are configured in Settings → Integrations.</p>
        </div>
      ) : null}
    </div>
  );
}

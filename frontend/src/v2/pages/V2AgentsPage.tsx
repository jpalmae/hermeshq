import { FormEvent, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { useAgents, useAgentAction, useCreateAgent } from "../../api/agents";
import { AgentAvatar } from "../../components/AgentAvatar";
import { useNodes } from "../../api/nodes";
import { useProviders } from "../../api/providers";
import { usePermissionPolicies } from "../../api/permissionPolicies";
import { useSecrets } from "../../api/secrets";
import { useSessionStore } from "../../stores/sessionStore";
import { v2toast, extractErrorMessage } from "../toast";
import { useI18n } from "../../lib/i18n";

function statusTone(status: string): "success" | "error" | "warn" | "neutral" {
  if (status === "running") return "success";
  if (status === "error") return "error";
  if (status === "starting" || status === "paused") return "warn";
  return "neutral";
}

function slugify(value: string) {
  return value
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "") || `agent-${Date.now()}`;
}

export function V2AgentsPage() {
  const { t } = useI18n();
  const { data: agents, isLoading } = useAgents();
  const { data: nodes } = useNodes();
  const { data: providers } = useProviders();
  const { data: secrets } = useSecrets();
  const startAgent = useAgentAction("start");
  const stopAgent = useAgentAction("stop");
  const createAgent = useCreateAgent();
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";

  const [filter, setFilter] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [friendlyName, setFriendlyName] = useState("");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [apiKeyRef, setApiKeyRef] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [runtimeType, setRuntimeType] = useState("hermes");
  const [permissionPolicyId, setPermissionPolicyId] = useState("");
  const { data: permissionPolicies } = usePermissionPolicies();

  const filtered = useMemo(() => {
    const list = agents ?? [];
    if (!filter.trim()) return list;
    const q = filter.toLowerCase();
    return list.filter(
      (a) =>
        a.name.toLowerCase().includes(q) ||
        (a.friendly_name ?? "").toLowerCase().includes(q) ||
        (a.model ?? "").toLowerCase().includes(q) ||
        a.status.toLowerCase().includes(q),
    );
  }, [agents, filter]);

  const selectedProvider = useMemo(
    () => (providers ?? []).find((p) => p.runtime_provider === provider || p.slug === provider),
    [providers, provider],
  );

  const providerSecrets = useMemo(
    () => (secrets ?? []).filter((s) => !selectedProvider || !s.provider || s.provider === selectedProvider.slug),
    [secrets, selectedProvider],
  );

  function resetCreateForm() {
    setFriendlyName("");
    setProvider("");
    setModel("");
    setApiKeyRef("");
    setBaseUrl("");
    setSystemPrompt("");
    setRuntimeType("hermes");
    setPermissionPolicyId("");
    setCreateError(null);
  }

  async function onCreateSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreateError(null);
    const nodeId = nodes?.[0]?.id;
    if (!nodeId) {
      setCreateError(t("v2.noComputeNode"));
      return;
    }
    const name = friendlyName.trim();
    if (!name) {
      setCreateError(t("v2.nameRequired"));
      return;
    }
    try {
      const payload: Record<string, unknown> = {
        node_id: nodeId,
        friendly_name: name,
        name,
        slug: slugify(name),
        run_mode: "hybrid",
        runtime_profile: "standard",
        runtime_type: runtimeType,
        use_provider_default: !provider,
      };
      if (runtimeType === "pi") {
        payload.pi_config = { tools: ["read", "bash", "edit"], thinking_level: "off", project_trust: "always" };
        payload.permission_policy_id = permissionPolicyId || null;
      }
      if (provider) {
        payload.provider = provider;
        payload.model = model || selectedProvider?.default_model || null;
        payload.api_key_ref = apiKeyRef || null;
        payload.base_url = baseUrl || selectedProvider?.base_url || null;
      }
      if (systemPrompt.trim()) {
        payload.system_prompt = systemPrompt.trim();
      }
      const created = await createAgent.mutateAsync(payload);
      v2toast.success(t("v2.agentCreated", { name }));
      resetCreateForm();
      setShowCreate(false);
      window.location.href = `/v2/agents/${created.id}`;
    } catch (error) {
      setCreateError(extractErrorMessage(error, t("v2.agentCreationFailed")));
    }
  }

  async function handleStart(agentId: string, name: string) {
    try {
      await startAgent.mutateAsync(agentId);
      v2toast.success(t("v2.agentStarted", { name }));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.failedToStart", { name })));
    }
  }

  async function handleStop(agentId: string, name: string) {
    try {
      await stopAgent.mutateAsync(agentId);
      v2toast.success(t("v2.agentStopped", { name }));
    } catch (error) {
      v2toast.error(extractErrorMessage(error, t("v2.failedToStop", { name })));
    }
  }

  return (
    <div>
      <div className="v2-page-header">
        <div>
          <h1 className="v2-page-title">{t("v2.agents")}</h1>
          <p className="v2-page-subtitle">{(agents ?? []).length} {t("v2.agentsConfigured")}</p>
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <input
            className="v2-input"
            style={{ width: 240 }}
            placeholder={t("v2.filterPlaceholder")}
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          {isAdmin ? (
            <button className="v2-btn v2-btn-primary" onClick={() => setShowCreate((v) => !v)}>
              {showCreate ? t("v2.close") : t("v2.newAgentBtn")}
            </button>
          ) : null}
        </div>
      </div>

      {showCreate && isAdmin ? (
        <form className="v2-card v2-section" onSubmit={onCreateSubmit}>
          <div className="v2-card-header">
            <h2 className="v2-card-title">{t("v2.createAgent")}</h2>
            <Link to="/builder" className="v2-btn v2-btn-ghost" style={{ fontSize: 12 }}>
              {t("v2.preferAiBuilder")}
            </Link>
          </div>
          <div className="v2-card-body" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20 }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              <div className="v2-field">
                <label className="v2-field-label">{t("v2.name")} *</label>
                <input className="v2-input" value={friendlyName} onChange={(e) => setFriendlyName(e.target.value)} required placeholder={t("v2.namePlaceholder")} autoFocus />
                <span className="v2-field-hint">{t("v2.slug")}: {friendlyName.trim() ? slugify(friendlyName) : "—"}</span>
              </div>
              <div className="v2-field">
                <label className="v2-field-label">{t("v2.runtimeType")}</label>
                <select className="v2-select" value={runtimeType} onChange={(e) => setRuntimeType(e.target.value)}>
                  <option value="hermes">{t("v2.runtimeHermes")}</option>
                  <option value="pi">{t("v2.runtimePi")}</option>
                </select>
              </div>
              {runtimeType === "pi" ? (
                <div className="v2-field">
                  <label className="v2-field-label">{t("v2.permissionPolicy")}</label>
                  <select className="v2-select" value={permissionPolicyId} onChange={(e) => setPermissionPolicyId(e.target.value)}>
                    <option value="">{t("v2.none")}</option>
                    {(permissionPolicies ?? []).map((p) => (
                      <option key={p.id} value={p.id}>{p.name}</option>
                    ))}
                  </select>
                </div>
              ) : null}
              <div className="v2-field">
                <label className="v2-field-label">{t("v2.providerPreset")}</label>
                <select
                  className="v2-select"
                  value={provider}
                  onChange={(e) => {
                    setProvider(e.target.value);
                    const p = (providers ?? []).find((pr) => pr.runtime_provider === e.target.value || pr.slug === e.target.value);
                    if (p?.default_model) setModel(p.default_model);
                    if (p?.base_url) setBaseUrl(p.base_url);
                  }}
                >
                  <option value="">{t("v2.useInstanceDefault")}</option>
                  {(providers ?? []).filter((p) => p.enabled).map((p) => (
                    <option key={p.slug} value={p.runtime_provider}>{p.name}</option>
                  ))}
                </select>
              </div>
              {provider ? (
                <div className="v2-field">
                  <label className="v2-field-label">{t("v2.model")}</label>
                  {selectedProvider?.available_models?.length ? (
                    <select className="v2-select" value={model} onChange={(e) => setModel(e.target.value)}>
                      {selectedProvider.available_models.map((m) => (
                        <option key={m} value={m}>{m}</option>
                      ))}
                    </select>
                  ) : (
                    <input className="v2-input" value={model} onChange={(e) => setModel(e.target.value)} placeholder={selectedProvider?.default_model ?? "model-id"} />
                  )}
                </div>
              ) : null}
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              {provider ? (
                <>
                  <div className="v2-field">
                    <label className="v2-field-label">{t("v2.apiKey")}</label>
                    <select className="v2-select" value={apiKeyRef} onChange={(e) => setApiKeyRef(e.target.value)}>
                      <option value="">{t("v2.none")}</option>
                      {providerSecrets.map((s) => (
                        <option key={s.name} value={s.name}>{s.name}</option>
                      ))}
                    </select>
                  </div>
                  <div className="v2-field">
                    <label className="v2-field-label">{t("v2.baseUrl")}</label>
                    <input className="v2-input" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={selectedProvider?.base_url ?? "https://api.example.com/v1"} />
                  </div>
                </>
              ) : null}
              <details className="v2-details">
                <summary>{t("v2.advanced")}</summary>
                <div className="v2-details-body" style={{ paddingTop: 14 }}>
                  <div className="v2-field">
                    <label className="v2-field-label">{t("v2.systemPrompt")}</label>
                    <textarea className="v2-textarea" rows={5} value={systemPrompt} onChange={(e) => setSystemPrompt(e.target.value)} placeholder={t("v2.systemPromptPlaceholder")} />
                  </div>
                </div>
              </details>
              {createError ? <p className="v2-field-error">{createError}</p> : null}
              <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
                <button type="submit" className="v2-btn v2-btn-primary" disabled={createAgent.isPending}>
                  {createAgent.isPending ? t("v2.creating") : t("v2.createAgent")}
                </button>
                <button type="button" className="v2-btn v2-btn-secondary" onClick={() => { resetCreateForm(); setShowCreate(false); }}>
                  {t("v2.cancel")}
                </button>
              </div>
            </div>
          </div>
        </form>
      ) : null}

      <section className="v2-card">
        {isLoading ? (
          <div style={{ padding: 20 }}>
            {[1, 2, 3, 4].map((i) => (
              <div key={i} className="v2-skeleton" style={{ height: 56, marginBottom: 8 }} />
            ))}
          </div>
        ) : filtered.length === 0 ? (
          <div className="v2-empty">
            <p className="v2-empty-title">{filter ? t("v2.noAgentsMatch") : t("v2.noAgentsYet")}</p>
            <p className="v2-empty-text">
              {filter ? t("v2.tryDifferentSearch") : t("v2.createFirstAgentShort")}
            </p>
            {!filter && isAdmin ? (
              <div className="v2-empty-action">
                <button className="v2-btn v2-btn-primary" onClick={() => setShowCreate(true)}>{t("v2.createAgent")}</button>
              </div>
            ) : null}
          </div>
        ) : (
          filtered.map((agent) => {
            const name = agent.friendly_name || agent.name;
            const isRunning = agent.status === "running";
            return (
              <div key={agent.id} className="v2-agent-row">
                <Link to={`/v2/agents/${agent.id}`} style={{ display: "flex", alignItems: "center", gap: 14, flex: 1, textDecoration: "none", color: "inherit", minWidth: 0 }}>
                  <AgentAvatar agent={agent} sizeClass="h-9 w-9" roundedClass="rounded-lg" />
                  <div style={{ minWidth: 0 }}>
                    <div className="v2-agent-name">{name}</div>
                    <div className="v2-agent-meta">
                      {agent.model ?? agent.provider} · {agent.slug}
                    </div>
                  </div>
                </Link>
                <span className="v2-pill" data-tone={statusTone(agent.status)}>
                  <span className="v2-pill-dot" />
                  {agent.status}
                </span>
                {isRunning ? (
                  <button className="v2-btn v2-btn-secondary" style={{ padding: "6px 12px", fontSize: 12.5 }} disabled={stopAgent.isPending} onClick={() => void handleStop(agent.id, name)}>
                    {t("v2.stop")}
                  </button>
                ) : (
                  <button className="v2-btn v2-btn-secondary" style={{ padding: "6px 12px", fontSize: 12.5 }} disabled={startAgent.isPending} onClick={() => void handleStart(agent.id, name)}>
                    {t("v2.start")}
                  </button>
                )}
              </div>
            );
          })
        )}
      </section>
    </div>
  );
}

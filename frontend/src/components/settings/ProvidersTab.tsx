import { useEffect, useState } from "react";

import { useProviders, useUpdateProvider, useRefreshProviderModels } from "../../api/providers";
import { useSecrets } from "../../api/secrets";
import { useI18n } from "../../lib/i18n";
import { useSessionStore } from "../../stores/sessionStore";

const NON_FETCHABLE = new Set(["bedrock"]);

function canFetchModels(provider: { auth_type: string; base_url: string | null; runtime_provider: string }): boolean {
  return provider.auth_type === "api_key" && Boolean(provider.base_url) && !NON_FETCHABLE.has(provider.runtime_provider);
}

export function ProvidersTab() {
  const currentUser = useSessionStore((state) => state.user);
  const { t } = useI18n();
  const { data: providers } = useProviders(Boolean(currentUser));
  const updateProvider = useUpdateProvider();
  const refreshModels = useRefreshProviderModels();
  const { data: secrets } = useSecrets(Boolean(currentUser));
  const [refreshStatus, setRefreshStatus] = useState<Record<string, { ok: boolean; msg: string } | null>>({});
  const [egressNotice, setEgressNotice] = useState<Record<string, string | null>>({});

  const [providerDrafts, setProviderDrafts] = useState<Record<string, {
    name: string;
    base_url: string;
    default_model: string;
    available_models: string;
    enabled: boolean;
    api_key_ref: string;
  }>>({});

  useEffect(() => {
    setProviderDrafts(
      Object.fromEntries(
        (providers ?? []).map((provider) => [
          provider.slug,
          {
            name: provider.name,
            base_url: provider.base_url ?? "",
            default_model: provider.default_model ?? "",
            available_models: (provider.available_models ?? []).join("\n"),
            enabled: provider.enabled,
            api_key_ref: provider.api_key_ref ?? "",
          },
        ]),
      ),
    );
  }, [providers]);

  async function saveProvider(providerSlug: string) {
    const draft = providerDrafts[providerSlug];
    if (!draft) {
      return;
    }
    await updateProvider.mutateAsync({
      providerSlug,
      payload: {
        name: draft.name,
        base_url: draft.base_url || null,
        default_model: draft.default_model || null,
        available_models: draft.available_models
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean),
        enabled: draft.enabled,
        api_key_ref: draft.api_key_ref || null,
      },
    }).then((saved) => {
      setEgressNotice((prev) => ({ ...prev, [providerSlug]: saved?.egress_notice ?? null }));
    }).catch(() => {
      setEgressNotice((prev) => ({ ...prev, [providerSlug]: null }));
    });
  }

  return (
    <section className="panel-frame p-6">
      <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
        <div>
          <p className="panel-label">{t("providers.registry")}</p>
          <h2 className="mt-2 text-3xl text-[var(--text-display)]">{t("providers.title")}</h2>
        </div>
        <p className="panel-label">{t("providers.configuredCount", { count: providers?.length ?? 0 })}</p>
      </div>
      <div className="mt-6 grid gap-4 xl:grid-cols-2">
        {(providers ?? []).map((provider) => {
          const draft = providerDrafts[provider.slug];
          if (!draft) return null;
  async function refreshProviderModels(providerSlug: string) {
    setRefreshStatus((prev) => ({ ...prev, [providerSlug]: null }));
    try {
      const result = await refreshModels.mutateAsync(providerSlug);
      setRefreshStatus((prev) => ({
        ...prev,
        [providerSlug]: { ok: true, msg: `${result.count} models fetched` },
      }));
    } catch (error) {
      const msg = error && typeof error === "object" && "response" in error
        ? (error as { response?: { data?: { detail?: string } } }).response?.data?.detail ?? "Refresh failed"
        : "Refresh failed";
      setRefreshStatus((prev) => ({ ...prev, [providerSlug]: { ok: false, msg } }));
    }
  }

  return (
            <article key={provider.slug} className="border border-[var(--border)] bg-[var(--surface-raised)] p-5">
              <div className="flex items-start justify-between gap-4 border-b border-[var(--border)] pb-4">
                <div>
                  <p className="panel-label">{provider.runtime_provider}</p>
                  <h3 className="mt-2 text-xl text-[var(--text-display)]">{provider.name}</h3>
                  <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">
                    {provider.description}
                  </p>
                </div>
                <label className="panel-field !mt-0 min-w-[7rem]">
                  <span className="panel-label">{t("providers.enabled")}</span>
                  <select
                    value={draft.enabled ? "true" : "false"}
                    onChange={(event) =>
                      setProviderDrafts((current) => ({
                        ...current,
                        [provider.slug]: {
                          ...current[provider.slug],
                          enabled: event.target.value === "true",
                        },
                      }))
                    }
                  >
                    <option value="true">{t("common.yes")}</option>
                    <option value="false">{t("common.no")}</option>
                  </select>
                </label>
              </div>

              <div className="mt-4 grid gap-4">
                <label className="panel-field">
                  <span className="panel-label">{t("providers.providerName")}</span>
                  <input
                    value={draft.name}
                    onChange={(event) =>
                      setProviderDrafts((current) => ({
                        ...current,
                        [provider.slug]: { ...current[provider.slug], name: event.target.value },
                      }))
                    }
                  />
                </label>
                <label className="panel-field">
                  <span className="panel-label">{t("agents.baseUrl")}</span>
                  <input
                    value={draft.base_url}
                    onChange={(event) =>
                      setProviderDrafts((current) => ({
                        ...current,
                        [provider.slug]: { ...current[provider.slug], base_url: event.target.value },
                      }))
                    }
                    disabled={!provider.supports_custom_base_url}
                  />
                </label>
                <label className="panel-field">
                  <span className="panel-label">{t("providers.defaultModel")}</span>
                  {(provider.available_models ?? []).length > 0 ? (
                    <select
                      value={draft.default_model}
                      onChange={(event) =>
                        setProviderDrafts((current) => ({
                          ...current,
                          [provider.slug]: { ...current[provider.slug], default_model: event.target.value },
                        }))
                      }
                    >
                      <option value="">— Select model —</option>
                      {(provider.available_models ?? []).map((model) => (
                        <option key={model} value={model}>{model}</option>
                      ))}
                    </select>
                  ) : (
                    <input
                      value={draft.default_model}
                      onChange={(event) =>
                        setProviderDrafts((current) => ({
                          ...current,
                          [provider.slug]: { ...current[provider.slug], default_model: event.target.value },
                        }))
                      }
                      placeholder="Refresh models first or type manually"
                    />
                  )}
                </label>
                <label className="panel-field">
                  <span className="panel-label">{t("providers.availableModels")}</span>
                  <textarea
                    className="min-h-[5rem] font-mono text-sm"
                    value={draft.available_models}
                    placeholder="model-1&#10;model-2&#10;model-3"
                    onChange={(event) =>
                      setProviderDrafts((current) => ({
                        ...current,
                        [provider.slug]: { ...current[provider.slug], available_models: event.target.value },
                      }))
                    }
                  />
                </label>
                {canFetchModels(provider) ? (
                  <div className="flex items-center gap-3">
                    <button
                      type="button"
                      className="panel-button-secondary text-sm"
                      onClick={() => void refreshProviderModels(provider.slug)}
                      disabled={refreshModels.isPending && refreshModels.variables === provider.slug}
                    >
                      {refreshModels.isPending && refreshModels.variables === provider.slug
                        ? "Fetching…"
                        : "↻ Refresh from API"}
                    </button>
                    {refreshStatus[provider.slug] ? (
                      <span
                        className="text-sm"
                        style={{ color: refreshStatus[provider.slug]!.ok ? "var(--success)" : "var(--danger)" }}
                      >
                        {refreshStatus[provider.slug]!.ok ? "✓ " : "✗ "}
                        {refreshStatus[provider.slug]!.msg}
                      </span>
                    ) : provider.models_refreshed_at ? (
                      <span className="text-xs text-[var(--text-disabled)]">
                        Last: {new Date(provider.models_refreshed_at).toLocaleString()}
                      </span>
                    ) : null}
                  </div>
                ) : null}
                {provider.supports_secret_ref && (secrets ?? []).length > 0 ? (
                  <label className="panel-field">
                    <span className="panel-label">API key (secret for model refresh)</span>
                    <select
                      value={draft.api_key_ref}
                      onChange={(event) =>
                        setProviderDrafts((current) => ({
                          ...current,
                          [provider.slug]: { ...current[provider.slug], api_key_ref: event.target.value },
                        }))
                      }
                    >
                      <option value="">None</option>
                      {(secrets ?? []).map((s) => (
                        <option key={s.name} value={s.name}>{s.name}</option>
                      ))}
                    </select>
                  </label>
                ) : null}
                <div className="grid gap-2 text-sm text-[var(--text-secondary)]">
                  <p>{t("providers.authType")}: {provider.auth_type}</p>
                  <p>{t("providers.secretUsage")}: {provider.supports_secret_ref ? t("providers.secretSupported") : t("providers.secretNotSupported")}</p>
                  {provider.docs_url ? (
                    <a className="text-[var(--text-display)] underline underline-offset-4" href={provider.docs_url} target="_blank" rel="noreferrer">
                      {t("providers.openDocs")}
                    </a>
                  ) : null}
                </div>
                <button type="button" className="panel-button-primary w-full" onClick={() => void saveProvider(provider.slug)}>
                  {t("providers.saveProvider")}
                </button>
                {egressNotice[provider.slug] ? (
                  <p
                    className="rounded border px-3 py-2 text-xs"
                    style={{ borderColor: "var(--warning, #d97706)", color: "var(--warning, #b45309)" }}
                  >
                    ⚿ {egressNotice[provider.slug]}
                  </p>
                ) : null}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}

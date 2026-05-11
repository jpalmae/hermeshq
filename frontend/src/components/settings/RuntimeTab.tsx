import { FormEvent, useEffect, useMemo, useState } from "react";

import { useBootstrapSystemOperator } from "../../api/agents";
import { useProviders } from "../../api/providers";
import { useHermesVersions } from "../../api/hermesVersions";
import { useRuntimeCapabilityOverview } from "../../api/runtimeProfiles";
import { useSecrets } from "../../api/secrets";
import {
  useSettings,
  useUpdateSettings,
} from "../../api/settings";
import { useI18n } from "../../lib/i18n";
import { applyProviderPreset, findMatchingProvider } from "../../lib/providers";
import { useSessionStore } from "../../stores/sessionStore";

export function RuntimeTab() {
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";
  const { t } = useI18n();
  const bootstrapSystemOperator = useBootstrapSystemOperator();
  const { data: secrets } = useSecrets(isAdmin);
  const { data: providers } = useProviders(Boolean(currentUser));
  const { data: hermesVersions } = useHermesVersions(isAdmin);
  const { data: runtimeCapabilityOverview } = useRuntimeCapabilityOverview(Boolean(currentUser));
  const { data: settings } = useSettings(isAdmin);
  const updateSettings = useUpdateSettings();

  const [defaultProvider, setDefaultProvider] = useState("");
  const [defaultModel, setDefaultModel] = useState("");
  const [defaultApiKeyRef, setDefaultApiKeyRef] = useState("");
  const [defaultBaseUrl, setDefaultBaseUrl] = useState("");
  const [defaultHermesVersion, setDefaultHermesVersion] = useState("bundled");
  const [selectedDefaultProviderSlug, setSelectedDefaultProviderSlug] = useState("");

  useEffect(() => {
    setDefaultProvider(settings?.default_provider ?? "");
    setDefaultModel(settings?.default_model ?? "");
    setDefaultApiKeyRef(settings?.default_api_key_ref ?? "");
    setDefaultBaseUrl(settings?.default_base_url ?? "");
    setDefaultHermesVersion(settings?.default_hermes_version ?? "bundled");
  }, [settings]);

  useEffect(() => {
    const match = findMatchingProvider(providers, settings?.default_provider, settings?.default_base_url);
    setSelectedDefaultProviderSlug(match?.slug ?? "");
  }, [providers, settings?.default_provider, settings?.default_base_url]);

  const enabledProviders = useMemo(
    () => (providers ?? []).filter((provider) => provider.enabled),
    [providers],
  );

  const selectedDefaultProvider = useMemo(
    () => enabledProviders.find((provider) => provider.slug === selectedDefaultProviderSlug) ?? null,
    [enabledProviders, selectedDefaultProviderSlug],
  );

  const systemOperator = useMemo(() => null, []);

  async function submitDefaults(event: FormEvent) {
    event.preventDefault();
    await updateSettings.mutateAsync({
      default_provider: defaultProvider || null,
      default_model: defaultModel || null,
      default_api_key_ref: defaultApiKeyRef || null,
      default_base_url: defaultBaseUrl || null,
      default_hermes_version: defaultHermesVersion === "bundled" ? null : defaultHermesVersion,
    });
  }

  return (
    <section className="grid gap-6 xl:grid-cols-3">
      <form className="panel-frame p-6" onSubmit={submitDefaults}>
        <p className="panel-label">{t("settings.runtimeDefaults")}</p>
        <h2 className="mt-2 text-2xl text-[var(--text-display)]">{t("settings.instanceProviders")}</h2>
        <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
          {t("settings.providersCopy")}
        </p>
        <div className="mt-6 space-y-4">
          <label className="panel-field">
            <span className="panel-label">{t("providers.catalogProvider")}</span>
            <select
              value={selectedDefaultProviderSlug}
              onChange={(event) => {
                const slug = event.target.value;
                setSelectedDefaultProviderSlug(slug);
                const provider = enabledProviders.find((item) => item.slug === slug);
                if (!provider) {
                  return;
                }
                const applied = applyProviderPreset(provider, defaultApiKeyRef);
                setDefaultProvider(applied.provider);
                setDefaultModel(applied.model);
                setDefaultBaseUrl(applied.base_url);
                if (!provider.supports_secret_ref) {
                  setDefaultApiKeyRef("");
                }
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
            <span className="panel-label">{t("agents.provider")}</span>
            <input value={defaultProvider} onChange={(event) => setDefaultProvider(event.target.value)} />
          </label>
          <label className="panel-field">
            <span className="panel-label">{t("agents.model")}</span>
            <input value={defaultModel} onChange={(event) => setDefaultModel(event.target.value)} />
          </label>
          <label className="panel-field">
            <span className="panel-label">{t("agents.secretRef")}</span>
            <select
              value={defaultApiKeyRef}
              onChange={(event) => setDefaultApiKeyRef(event.target.value)}
              disabled={selectedDefaultProvider?.supports_secret_ref === false}
            >
              <option value="">{selectedDefaultProvider?.supports_secret_ref === false ? t("providers.oauthManaged") : t("providers.noSecret")}</option>
              {(secrets ?? []).map((secret) => (
                <option key={String(secret.id)} value={String(secret.name)}>
                  {String(secret.name)}{secret.provider ? ` (${secret.provider})` : ""}
                </option>
              ))}
            </select>
          </label>
          <label className="panel-field">
            <span className="panel-label">{t("agents.baseUrl")}</span>
            <input value={defaultBaseUrl} onChange={(event) => setDefaultBaseUrl(event.target.value)} />
          </label>
          <label className="panel-field">
            <span className="panel-label">Default Hermes version</span>
            <select value={defaultHermesVersion} onChange={(event) => setDefaultHermesVersion(event.target.value)}>
              <option value="bundled">Bundled runtime</option>
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
          <button className="panel-button-primary w-full" type="submit">
            {t("settings.saveRuntimeDefaults")}
          </button>
          {selectedDefaultProvider ? (
            <p className="text-sm leading-6 text-[var(--text-secondary)]">
              {selectedDefaultProvider.description}
            </p>
          ) : null}
        </div>
      </form>

      <div className="panel-frame p-6">
        <p className="panel-label">Instance defaults</p>
        <div className="mt-4 space-y-3">
          <div className="border-b border-[var(--border)] pb-3">
            <p className="panel-label">Provider</p>
            <p className="mt-2 text-sm text-[var(--text-display)]">{String(settings?.default_provider ?? "unset")}</p>
          </div>
          <div className="border-b border-[var(--border)] pb-3">
            <p className="panel-label">Model</p>
            <p className="mt-2 text-sm text-[var(--text-display)]">{String(settings?.default_model ?? "unset")}</p>
          </div>
          <div className="border-b border-[var(--border)] pb-3">
            <p className="panel-label">Secret ref</p>
            <p className="mt-2 text-sm text-[var(--text-display)]">{String(settings?.default_api_key_ref ?? "unset")}</p>
          </div>
          <div className="border-b border-[var(--border)] pb-3">
            <p className="panel-label">Base URL</p>
            <p className="mt-2 break-all text-sm text-[var(--text-display)]">{String(settings?.default_base_url ?? "unset")}</p>
          </div>
          <div className="pb-3">
            <p className="panel-label">Hermes version</p>
            <p className="mt-2 text-sm text-[var(--text-display)]">{String(settings?.default_hermes_version ?? "bundled")}</p>
          </div>
        </div>
        <div className="mt-6 border-t border-[var(--border)] pt-6">
          <p className="panel-label">{t("settings.hqOperator")}</p>
          <h3 className="mt-2 text-lg text-[var(--text-display)]">{t("settings.hqOperatorTitle")}</h3>
          <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
            {t("settings.hqOperatorCopy")}
          </p>
          <div className="mt-4 flex items-center gap-3">
            <button
              className="panel-button-primary"
              type="button"
              onClick={() => bootstrapSystemOperator.mutate()}
              disabled={bootstrapSystemOperator.isPending}
            >
              {bootstrapSystemOperator.isPending
                ? t("settings.hqOperatorCreating")
                : t("settings.hqOperatorCreate")}
            </button>
            <p className="panel-inline-status">
              {t("settings.hqOperatorHint")}
            </p>
          </div>
          {bootstrapSystemOperator.error ? (
            <p className="mt-3 text-sm text-[var(--danger)]">
              {bootstrapSystemOperator.error instanceof Error ? bootstrapSystemOperator.error.message : t("settings.hqOperatorFailed")}
            </p>
          ) : null}
        </div>
      </div>

      <div className="panel-frame p-6 xl:col-span-3">
        <p className="panel-label">{t("settings.runtimeBuiltins")}</p>
        <h2 className="mt-2 text-2xl text-[var(--text-display)]">{t("settings.runtimeBuiltinsTitle")}</h2>
        <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
          {t("settings.runtimeBuiltinsCopy")}
        </p>
        <div className="mt-6 grid gap-6 xl:grid-cols-[1.3fr_0.7fr]">
          <div className="grid gap-4 lg:grid-cols-3">
            {(runtimeCapabilityOverview?.profiles ?? []).map((profile) => (
              <article key={profile.slug} className="border border-[var(--border)] bg-[var(--surface-raised)] p-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <p className="panel-label">{profile.slug}</p>
                    <h3 className="mt-2 text-lg text-[var(--text-display)]">{profile.name}</h3>
                  </div>
                  <span className="rounded-full border border-[var(--border)] px-3 py-1 text-xs text-[var(--text-secondary)]">
                    {profile.terminal_allowed ? t("settings.terminalEnabled") : t("settings.terminalDisabled")}
                  </span>
                </div>
                <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                  {profile.tooling_summary}
                </p>
                {profile.phase1_full_access ? (
                  <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                    {t("settings.phase1FullAccess")}
                  </p>
                ) : (
                  <div className="mt-4 flex flex-wrap gap-2">
                    {profile.builtin_toolsets.map((toolset) => (
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
              </article>
            ))}
          </div>
          <div className="border border-[var(--border)] bg-[var(--surface-raised)] p-4">
            <p className="panel-label">{t("settings.platformPlugins")}</p>
            <div className="mt-4 space-y-4">
              {(runtimeCapabilityOverview?.platform_plugins ?? []).map((plugin) => (
                <article key={plugin.slug} className="border-b border-[var(--border)] pb-4 last:border-b-0 last:pb-0">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="panel-label">{plugin.toolset}</p>
                      <h3 className="mt-2 text-base text-[var(--text-display)]">{plugin.name}</h3>
                    </div>
                    <span className="rounded-full border border-[var(--border)] px-3 py-1 text-xs text-[var(--text-secondary)]">
                      {plugin.standard_compatible ? t("settings.standardCompatible") : t("settings.technicalOnly")}
                    </span>
                  </div>
                  <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                    {plugin.description}
                  </p>
                </article>
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
export default RuntimeTab;

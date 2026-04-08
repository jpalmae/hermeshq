import { FormEvent, useEffect, useMemo, useState } from "react";

import { useAgents } from "../api/agents";
import { useInstallIntegrationPackage, useIntegrationPackages, useUninstallIntegrationPackage, useUploadIntegrationPackage } from "../api/integrationPackages";
import { useProviders, useUpdateProvider } from "../api/providers";
import { useCreateSecret, useSecrets } from "../api/secrets";
import {
  resolveAssetUrl,
  useDeleteBrandAsset,
  useDeleteTuiSkin,
  useSettings,
  useUpdateSettings,
  useUploadBrandAsset,
  useUploadTuiSkin,
} from "../api/settings";
import { useCreateTemplate, useTemplates } from "../api/templates";
import { useI18n } from "../lib/i18n";
import { applyProviderPreset, findMatchingProvider } from "../lib/providers";
import { useSessionStore } from "../stores/sessionStore";

export function SettingsPage() {
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";
  const { t } = useI18n();
  const { data: agents } = useAgents();
  const { data: secrets } = useSecrets(isAdmin);
  const { data: providers } = useProviders(Boolean(currentUser));
  const { data: integrationPackages } = useIntegrationPackages(isAdmin);
  const { data: templates } = useTemplates(isAdmin);
  const { data: settings } = useSettings(isAdmin);
  const updateProvider = useUpdateProvider();
  const createSecret = useCreateSecret();
  const createTemplate = useCreateTemplate();
  const updateSettings = useUpdateSettings();
  const uploadLogo = useUploadBrandAsset("logo");
  const uploadFavicon = useUploadBrandAsset("favicon");
  const deleteLogo = useDeleteBrandAsset("logo");
  const deleteFavicon = useDeleteBrandAsset("favicon");
  const uploadTuiSkin = useUploadTuiSkin();
  const deleteTuiSkin = useDeleteTuiSkin();
  const uploadIntegrationPackage = useUploadIntegrationPackage();
  const installIntegrationPackage = useInstallIntegrationPackage();
  const uninstallIntegrationPackage = useUninstallIntegrationPackage();

  const [appName, setAppName] = useState("");
  const [appShortName, setAppShortName] = useState("");
  const [themeMode, setThemeMode] = useState<"dark" | "light" | "system">("dark");
  const [defaultLocale, setDefaultLocale] = useState<"en" | "es">("en");
  const [secretName, setSecretName] = useState("");
  const [secretProvider, setSecretProvider] = useState("");
  const [secretValue, setSecretValue] = useState("");

  const [defaultProvider, setDefaultProvider] = useState("");
  const [defaultModel, setDefaultModel] = useState("");
  const [defaultApiKeyRef, setDefaultApiKeyRef] = useState("");
  const [defaultBaseUrl, setDefaultBaseUrl] = useState("");
  const [selectedDefaultProviderSlug, setSelectedDefaultProviderSlug] = useState("");
  const [providerDrafts, setProviderDrafts] = useState<Record<string, {
    name: string;
    base_url: string;
    default_model: string;
    enabled: boolean;
  }>>({});

  const [templateName, setTemplateName] = useState("");
  const [templateDescription, setTemplateDescription] = useState("");

  useEffect(() => {
    setAppName(settings?.app_name ?? "");
    setAppShortName(settings?.app_short_name ?? "");
    setThemeMode(settings?.theme_mode ?? "dark");
    setDefaultLocale(settings?.default_locale ?? "en");
    setDefaultProvider(settings?.default_provider ?? "");
    setDefaultModel(settings?.default_model ?? "");
    setDefaultApiKeyRef(settings?.default_api_key_ref ?? "");
    setDefaultBaseUrl(settings?.default_base_url ?? "");
  }, [settings]);

  useEffect(() => {
    const match = findMatchingProvider(providers, settings?.default_provider, settings?.default_base_url);
    setSelectedDefaultProviderSlug(match?.slug ?? "");
  }, [providers, settings?.default_provider, settings?.default_base_url]);

  useEffect(() => {
    setProviderDrafts(
      Object.fromEntries(
        (providers ?? []).map((provider) => [
          provider.slug,
          {
            name: provider.name,
            base_url: provider.base_url ?? "",
            default_model: provider.default_model ?? "",
            enabled: provider.enabled,
          },
        ]),
      ),
    );
  }, [providers]);

  const enabledProviders = useMemo(
    () => (providers ?? []).filter((provider) => provider.enabled),
    [providers],
  );
  const integrationCatalog = useMemo(
    () => integrationPackages ?? [],
    [integrationPackages],
  );

  const selectedDefaultProvider = useMemo(
    () => enabledProviders.find((provider) => provider.slug === selectedDefaultProviderSlug) ?? null,
    [enabledProviders, selectedDefaultProviderSlug],
  );

  async function submitSecret(event: FormEvent) {
    event.preventDefault();
    await createSecret.mutateAsync({
      name: secretName,
      provider: secretProvider || null,
      value: secretValue,
    });
    setSecretName("");
    setSecretProvider("");
    setSecretValue("");
  }

  async function submitTemplate(event: FormEvent) {
    event.preventDefault();
    await createTemplate.mutateAsync({
      name: templateName,
      description: templateDescription,
      config: {
        node_id: agents?.[0]?.id,
        name: `${templateName} Agent`,
        slug: templateName.toLowerCase().replace(/\s+/g, "-"),
        run_mode: "hybrid",
        model: defaultModel || undefined,
        provider: defaultProvider || undefined,
        api_key_ref: defaultApiKeyRef || undefined,
        base_url: defaultBaseUrl || undefined,
      },
    });
    setTemplateName("");
    setTemplateDescription("");
  }

  async function submitBranding(event: FormEvent) {
    event.preventDefault();
    await updateSettings.mutateAsync({
      app_name: appName || null,
      app_short_name: appShortName || null,
      theme_mode: themeMode,
      default_locale: defaultLocale,
    });
  }

  async function submitDefaults(event: FormEvent) {
    event.preventDefault();
    await updateSettings.mutateAsync({
      default_provider: defaultProvider || null,
      default_model: defaultModel || null,
      default_api_key_ref: defaultApiKeyRef || null,
      default_base_url: defaultBaseUrl || null,
    });
  }

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
        enabled: draft.enabled,
      },
    });
  }

  async function onLogoSelected(file: File | null) {
    if (!file) return;
    try {
      await uploadLogo.mutateAsync(file);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Logo upload failed");
    }
  }

  async function onFaviconSelected(file: File | null) {
    if (!file) return;
    try {
      await uploadFavicon.mutateAsync(file);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Favicon upload failed");
    }
  }

  async function onTuiSkinSelected(file: File | null) {
    if (!file) return;
    try {
      await uploadTuiSkin.mutateAsync(file);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "TUI skin upload failed");
    }
  }

  async function onIntegrationPackageSelected(file: File | null) {
    if (!file) return;
    try {
      await uploadIntegrationPackage.mutateAsync(file);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Integration package upload failed");
    }
  }

  const logoUrl = resolveAssetUrl(settings?.logo_url);
  const faviconUrl = resolveAssetUrl(settings?.favicon_url);

  if (currentUser && !isAdmin) {
    return (
      <section className="panel-frame p-6">
        <p className="panel-label">{t("settings.settings")}</p>
        <h2 className="mt-2 text-3xl text-[var(--text-display)]">{t("settings.adminRequired")}</h2>
        <p className="mt-4 max-w-[42rem] text-sm leading-6 text-[var(--text-secondary)]">
          {t("settings.adminCopy")}
        </p>
      </section>
    );
  }

  return (
    <div className="grid gap-6">
      <section className="grid gap-6 xl:grid-cols-4">
        <form className="panel-frame p-6" onSubmit={submitBranding}>
          <p className="panel-label">{t("settings.branding")}</p>
          <h2 className="mt-2 text-2xl text-[var(--text-display)]">{t("settings.instanceIdentity")}</h2>
          <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
            {t("settings.identityCopy")}
          </p>
          <div className="mt-6 space-y-4">
            <label className="panel-field">
              <span className="panel-label">{t("settings.appName")}</span>
              <input value={appName} onChange={(event) => setAppName(event.target.value)} placeholder="HermesHQ" />
            </label>
            <label className="panel-field">
              <span className="panel-label">{t("settings.shortName")}</span>
              <input value={appShortName} onChange={(event) => setAppShortName(event.target.value)} placeholder="HQ" />
            </label>
            <label className="panel-field">
              <span className="panel-label">{t("settings.theme")}</span>
              <select value={themeMode} onChange={(event) => setThemeMode(event.target.value as "dark" | "light" | "system")}>
                <option value="dark">{t("common.dark")}</option>
                <option value="light">{t("common.light")}</option>
                <option value="system">{t("common.system")}</option>
              </select>
            </label>
            <label className="panel-field">
              <span className="panel-label">{t("settings.language")}</span>
              <select value={defaultLocale} onChange={(event) => setDefaultLocale(event.target.value as "en" | "es")}>
                <option value="en">{t("common.english")}</option>
                <option value="es">{t("common.spanish")}</option>
              </select>
            </label>
            <button className="panel-button-primary w-full" type="submit">
              {t("settings.saveBranding")}
            </button>
          </div>
        </form>

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

        <section className="panel-frame p-6">
          <p className="panel-label">{t("settings.activeBranding")}</p>
          <h2 className="mt-2 text-2xl text-[var(--text-display)]">{t("settings.assets")}</h2>
          <div className="mt-6 space-y-5">
            <div className="border-b border-[var(--border)] pb-4">
              <p className="panel-label">{t("settings.logo")}</p>
              <div className="mt-3 flex items-center gap-3">
                {logoUrl ? (
                  <img src={logoUrl} alt={settings?.app_name ?? "Logo"} className="h-12 w-auto max-w-[8rem] object-contain" />
                ) : (
                  <p className="text-sm text-[var(--text-secondary)]">{t("settings.noLogo")}</p>
                )}
              </div>
              <div className="mt-4 flex flex-wrap gap-3">
                <label className="panel-button-secondary cursor-pointer">
                  Upload PNG
                  <input
                    className="hidden"
                    type="file"
                    accept="image/png"
                    onChange={(event) => void onLogoSelected(event.target.files?.[0] ?? null)}
                  />
                </label>
                <button
                  type="button"
                  className="panel-button-secondary"
                  onClick={async () => {
                    try {
                      await deleteLogo.mutateAsync();
                    } catch (error) {
                      window.alert(error instanceof Error ? error.message : "Logo removal failed");
                    }
                  }}
                  disabled={!settings?.has_logo}
                >
                  {t("settings.removeLogo")}
                </button>
              </div>
            </div>

            <div className="border-b border-[var(--border)] pb-4">
              <p className="panel-label">{t("settings.favicon")}</p>
              <div className="mt-3 flex items-center gap-3">
                {faviconUrl ? (
                  <img src={faviconUrl} alt="Favicon" className="h-10 w-10 rounded border border-[var(--border)] object-contain" />
                ) : (
                  <p className="text-sm text-[var(--text-secondary)]">{t("settings.noFavicon")}</p>
                )}
              </div>
              <div className="mt-4 flex flex-wrap gap-3">
                <label className="panel-button-secondary cursor-pointer">
                  Upload PNG/ICO
                  <input
                    className="hidden"
                    type="file"
                    accept="image/png,.ico,image/x-icon,image/vnd.microsoft.icon"
                    onChange={(event) => void onFaviconSelected(event.target.files?.[0] ?? null)}
                  />
                </label>
                <button
                  type="button"
                  className="panel-button-secondary"
                  onClick={async () => {
                    try {
                      await deleteFavicon.mutateAsync();
                    } catch (error) {
                      window.alert(error instanceof Error ? error.message : "Favicon removal failed");
                    }
                  }}
                  disabled={!settings?.has_favicon}
                >
                  {t("settings.removeFavicon")}
                </button>
              </div>
            </div>

            <div className="border-b border-[var(--border)] pb-4">
              <p className="panel-label">{t("settings.tuiSkin")}</p>
              <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">
                {t("settings.tuiSkinCopy")}
              </p>
              <div className="mt-3 flex items-center gap-3">
                {settings?.has_tui_skin ? (
                  <div>
                    <p className="text-sm text-[var(--text-display)]">
                      {String(settings?.default_tui_skin ?? "unset")}
                    </p>
                    <p className="mt-1 text-xs uppercase tracking-[0.24em] text-[var(--text-secondary)]">
                      {String(settings?.tui_skin_filename ?? "")}
                    </p>
                  </div>
                ) : (
                  <p className="text-sm text-[var(--text-secondary)]">{t("settings.noTuiSkin")}</p>
                )}
              </div>
              <div className="mt-4 flex flex-wrap gap-3">
                <label className="panel-button-secondary cursor-pointer">
                  {t("settings.uploadTuiSkin")}
                  <input
                    className="hidden"
                    type="file"
                    accept=".yaml,.yml,text/yaml,application/yaml"
                    onChange={(event) => void onTuiSkinSelected(event.target.files?.[0] ?? null)}
                  />
                </label>
                <button
                  type="button"
                  className="panel-button-secondary"
                  onClick={async () => {
                    try {
                      await deleteTuiSkin.mutateAsync();
                    } catch (error) {
                      window.alert(error instanceof Error ? error.message : "TUI skin removal failed");
                    }
                  }}
                  disabled={!settings?.has_tui_skin}
                >
                  {t("settings.removeTuiSkin")}
                </button>
              </div>
            </div>

            <div className="border-b border-[var(--border)] pb-3">
              <p className="panel-label">Current app name</p>
              <p className="mt-2 text-sm text-[var(--text-display)]">{String(settings?.app_name ?? "HermesHQ")}</p>
            </div>
            <div className="pb-3">
              <p className="panel-label">Current short name</p>
              <p className="mt-2 text-sm text-[var(--text-display)]">{String(settings?.app_short_name ?? settings?.app_name ?? "HermesHQ")}</p>
            </div>
            <div className="pb-3">
              <p className="panel-label">Current default theme</p>
              <p className="mt-2 text-sm text-[var(--text-display)]">{String(settings?.theme_mode ?? "dark")}</p>
            </div>
            <div className="pb-3">
              <p className="panel-label">{t("settings.currentTuiSkin")}</p>
              <p className="mt-2 text-sm text-[var(--text-display)]">
                {String(settings?.default_tui_skin ?? t("settings.hermesDefaultSkin"))}
              </p>
            </div>
          </div>
        </section>

        <form className="panel-frame p-6" onSubmit={submitSecret}>
          <p className="panel-label">Secrets</p>
          <h2 className="mt-2 text-2xl text-[var(--text-display)]">Vault</h2>
          <div className="mt-6 space-y-4">
            <label className="panel-field">
              <span className="panel-label">Name</span>
              <input value={secretName} onChange={(event) => setSecretName(event.target.value)} />
            </label>
            <label className="panel-field">
              <span className="panel-label">Provider</span>
              <select value={secretProvider} onChange={(event) => setSecretProvider(event.target.value)}>
                <option value="">{t("providers.genericSecret")}</option>
                {(providers ?? []).map((provider) => (
                  <option key={provider.slug} value={provider.slug}>
                    {provider.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="panel-field">
              <span className="panel-label">Value</span>
              <input value={secretValue} onChange={(event) => setSecretValue(event.target.value)} />
            </label>
            <button className="panel-button-primary w-full" type="submit">
              Store secret
            </button>
          </div>
        </form>

        <form className="panel-frame p-6" onSubmit={submitTemplate}>
          <p className="panel-label">Templates</p>
          <h2 className="mt-2 text-2xl text-[var(--text-display)]">Agent presets</h2>
          <div className="mt-6 space-y-4">
            <label className="panel-field">
              <span className="panel-label">Name</span>
              <input value={templateName} onChange={(event) => setTemplateName(event.target.value)} />
            </label>
            <label className="panel-field">
              <span className="panel-label">Description</span>
              <textarea rows={4} value={templateDescription} onChange={(event) => setTemplateDescription(event.target.value)} />
            </label>
            <button className="panel-button-primary w-full" type="submit">
              Save template
            </button>
          </div>
        </form>
      </section>

      <section className="grid gap-6 xl:grid-cols-3">
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
            <div className="pb-3">
              <p className="panel-label">Base URL</p>
              <p className="mt-2 break-all text-sm text-[var(--text-display)]">{String(settings?.default_base_url ?? "unset")}</p>
            </div>
          </div>
        </div>
        <div className="panel-frame p-6">
          <p className="panel-label">Stored secrets</p>
          <div className="mt-4 space-y-3">
            {(secrets ?? []).map((secret) => (
              <div key={String(secret.id)} className="border-b border-[var(--border)] pb-3">
                <p className="panel-label">{String(secret.provider ?? "generic")}</p>
                <p className="mt-2 text-sm text-[var(--text-display)]">{String(secret.name)}</p>
              </div>
            ))}
          </div>
        </div>
        <div className="panel-frame p-6">
          <p className="panel-label">Stored templates</p>
          <div className="mt-4 space-y-3">
            {(templates ?? []).map((template) => (
              <div key={String(template.id)} className="border-b border-[var(--border)] pb-3">
                <p className="mt-2 text-sm text-[var(--text-display)]">{String(template.name)}</p>
                <p className="mt-1 text-sm text-[var(--text-secondary)]">
                  {String(template.description ?? "")}
                </p>
              </div>
            ))}
          </div>
        </div>
        <div className="panel-frame p-6">
          <p className="panel-label">{t("settings.integrations")}</p>
          <h2 className="mt-2 text-2xl text-[var(--text-display)]">{t("settings.integrationCatalog")}</h2>
          <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
            {t("settings.integrationsCopy")}
          </p>
          <div className="mt-4 border-b border-[var(--border)] pb-4">
            <label className="panel-button-secondary cursor-pointer">
              {t("settings.uploadIntegrationPackage")}
              <input
                className="hidden"
                type="file"
                accept=".tar,.tar.gz,.tgz"
                onChange={(event) => void onIntegrationPackageSelected(event.target.files?.[0] ?? null)}
              />
            </label>
          </div>
          <div className="mt-4 space-y-4">
            {integrationCatalog.length ? (
              integrationCatalog.map((integration) => (
                <article key={integration.slug} className="border border-[var(--border)] bg-[var(--surface-raised)] p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <p className="panel-label">{integration.slug}</p>
                      <h3 className="mt-2 text-lg text-[var(--text-display)]">{integration.name}</h3>
                    </div>
                    <div className="text-right">
                      <p className="panel-label">{integration.installed ? t("settings.installed") : t("settings.available")}</p>
                      <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">{integration.source_type}</p>
                    </div>
                  </div>
                  <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                    {integration.plugin_description ?? integration.description}
                  </p>
                  <div className="mt-4 flex flex-wrap gap-2">
                    {integration.tools.map((tool) => (
                      <span
                        key={tool}
                        className="rounded-full border border-[var(--border)] px-3 py-1 font-mono text-xs text-[var(--text-secondary)]"
                      >
                        {tool}
                      </span>
                    ))}
                  </div>
                  <div className="mt-4 space-y-1 text-sm text-[var(--text-secondary)]">
                    <p>{t("agent.integrationSkill", { value: integration.skill_identifier ?? t("agent.none") })}</p>
                    <p>{t("agent.integrationSecretProvider", { value: integration.secret_provider ?? t("agent.none") })}</p>
                    <p>{t("agent.integrationProfiles", { value: integration.supported_profiles.join(", ") })}</p>
                    <p>{t("agent.integrationFields", { value: integration.required_fields.join(", ") })}</p>
                  </div>
                  <div className="mt-4 flex flex-wrap gap-3">
                    {integration.installed ? (
                      <button
                        type="button"
                        className="panel-button-secondary"
                        onClick={() => void uninstallIntegrationPackage.mutateAsync(integration.slug)}
                      >
                        {t("settings.uninstallIntegration")}
                      </button>
                    ) : (
                      <button
                        type="button"
                        className="panel-button-secondary"
                        onClick={() => void installIntegrationPackage.mutateAsync(integration.slug)}
                      >
                        {t("settings.installIntegration")}
                      </button>
                    )}
                  </div>
                </article>
              ))
            ) : (
              <p className="panel-inline-status">{t("settings.noIntegrations")}</p>
            )}
          </div>
        </div>
      </section>

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
                    <input
                      value={draft.default_model}
                      onChange={(event) =>
                        setProviderDrafts((current) => ({
                          ...current,
                          [provider.slug]: { ...current[provider.slug], default_model: event.target.value },
                        }))
                      }
                    />
                  </label>
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
                </div>
              </article>
            );
          })}
        </div>
      </section>
    </div>
  );
}

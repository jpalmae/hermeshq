import { FormEvent, useEffect, useMemo, useState } from "react";

import { useAgents, useBootstrapSystemOperator } from "../api/agents";
import {
  useCreateHermesVersion,
  useDeleteHermesVersionCatalogEntry,
  useHermesVersions,
  useInstallHermesVersion,
  useUninstallHermesVersion,
  useUpdateHermesVersion,
} from "../api/hermesVersions";
import {
  useInstallIntegrationPackage,
  useIntegrationPackages,
  useUninstallIntegrationPackage,
  useUploadIntegrationPackage,
} from "../api/integrationPackages";
import {
  useCreateIntegrationDraft,
  useIntegrationDraftFile,
  useIntegrationDrafts,
  useDeleteIntegrationDraft,
  useDeleteIntegrationDraftFile,
  usePublishIntegrationDraft,
  useSaveIntegrationDraftFile,
  useUpdateIntegrationDraft,
  useValidateIntegrationDraft,
} from "../api/integrationFactory";
import { useProviders, useUpdateProvider } from "../api/providers";
import { useRuntimeCapabilityOverview } from "../api/runtimeProfiles";
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

type SettingsTab = "general" | "runtime" | "providers" | "integrations" | "factory" | "hermesVersions" | "secrets" | "templates";

const SETTINGS_TAB_STORAGE_KEY = "hermeshq.settings.activeTab";

export function SettingsPage() {
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";
  const { t } = useI18n();
  const { data: agents } = useAgents();
  const bootstrapSystemOperator = useBootstrapSystemOperator();
  const { data: secrets } = useSecrets(isAdmin);
  const { data: providers } = useProviders(Boolean(currentUser));
  const { data: hermesVersions } = useHermesVersions(isAdmin);
  const { data: runtimeCapabilityOverview } = useRuntimeCapabilityOverview(Boolean(currentUser));
  const { data: integrationPackages } = useIntegrationPackages(isAdmin);
  const { data: integrationDrafts } = useIntegrationDrafts(isAdmin);
  const { data: templates } = useTemplates(isAdmin);
  const { data: settings } = useSettings(isAdmin);
  const updateProvider = useUpdateProvider();
  const createSecret = useCreateSecret();
  const createTemplate = useCreateTemplate();
  const updateSettings = useUpdateSettings();
  const createHermesVersion = useCreateHermesVersion();
  const installHermesVersion = useInstallHermesVersion();
  const updateHermesVersion = useUpdateHermesVersion();
  const uninstallHermesVersion = useUninstallHermesVersion();
  const deleteHermesVersionCatalogEntry = useDeleteHermesVersionCatalogEntry();
  const uploadLogo = useUploadBrandAsset("logo");
  const uploadFavicon = useUploadBrandAsset("favicon");
  const deleteLogo = useDeleteBrandAsset("logo");
  const deleteFavicon = useDeleteBrandAsset("favicon");
  const uploadTuiSkin = useUploadTuiSkin();
  const deleteTuiSkin = useDeleteTuiSkin();
  const uploadIntegrationPackage = useUploadIntegrationPackage();
  const installIntegrationPackage = useInstallIntegrationPackage();
  const uninstallIntegrationPackage = useUninstallIntegrationPackage();
  const createIntegrationDraft = useCreateIntegrationDraft();
  const updateIntegrationDraft = useUpdateIntegrationDraft();
  const saveIntegrationDraftFile = useSaveIntegrationDraftFile();
  const deleteIntegrationDraftFile = useDeleteIntegrationDraftFile();
  const validateIntegrationDraft = useValidateIntegrationDraft();
  const publishIntegrationDraft = usePublishIntegrationDraft();
  const deleteIntegrationDraft = useDeleteIntegrationDraft();

  const [appName, setAppName] = useState("");
  const [appShortName, setAppShortName] = useState("");
  const [themeMode, setThemeMode] = useState<"dark" | "light" | "system" | "enterprise" | "sixmanager">("dark");
  const [defaultLocale, setDefaultLocale] = useState<"en" | "es">("en");
  const [secretName, setSecretName] = useState("");
  const [secretProvider, setSecretProvider] = useState("");
  const [secretValue, setSecretValue] = useState("");

  const [defaultProvider, setDefaultProvider] = useState("");
  const [defaultModel, setDefaultModel] = useState("");
  const [defaultApiKeyRef, setDefaultApiKeyRef] = useState("");
  const [defaultBaseUrl, setDefaultBaseUrl] = useState("");
  const [defaultHermesVersion, setDefaultHermesVersion] = useState("bundled");
  const [newHermesVersion, setNewHermesVersion] = useState("");
  const [newHermesReleaseTag, setNewHermesReleaseTag] = useState("");
  const [newHermesDescription, setNewHermesDescription] = useState("");
  const [hermesVersionDrafts, setHermesVersionDrafts] = useState<Record<string, {
    release_tag: string;
    description: string;
  }>>({});
  const [selectedDefaultProviderSlug, setSelectedDefaultProviderSlug] = useState("");
  const [providerDrafts, setProviderDrafts] = useState<Record<string, {
    name: string;
    base_url: string;
    default_model: string;
    enabled: boolean;
  }>>({});

  const [templateName, setTemplateName] = useState("");
  const [templateDescription, setTemplateDescription] = useState("");
  const [activeTab, setActiveTab] = useState<SettingsTab>("general");
  const [draftSlug, setDraftSlug] = useState("");
  const [draftName, setDraftName] = useState("");
  const [draftDescription, setDraftDescription] = useState("");
  const [draftTemplate, setDraftTemplate] = useState<"rest-api" | "empty">("rest-api");
  const [selectedDraftId, setSelectedDraftId] = useState<string | null>(null);
  const [selectedDraftPath, setSelectedDraftPath] = useState<string | null>(null);
  const [draftMetaName, setDraftMetaName] = useState("");
  const [draftMetaDescription, setDraftMetaDescription] = useState("");
  const [draftMetaVersion, setDraftMetaVersion] = useState("0.1.0");
  const [draftNotes, setDraftNotes] = useState("");
  const [draftEditorContent, setDraftEditorContent] = useState("");
  const [newDraftFilePath, setNewDraftFilePath] = useState("");

  useEffect(() => {
    setAppName(settings?.app_name ?? "");
    setAppShortName(settings?.app_short_name ?? "");
    setThemeMode(settings?.theme_mode ?? "dark");
    setDefaultLocale(settings?.default_locale ?? "en");
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

  useEffect(() => {
    setHermesVersionDrafts(
      Object.fromEntries(
        (hermesVersions ?? []).map((version) => [
          version.version,
          {
            release_tag: version.release_tag ?? "",
            description: version.description ?? "",
          },
        ]),
      ),
    );
  }, [hermesVersions]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const stored = window.localStorage.getItem(SETTINGS_TAB_STORAGE_KEY);
    if (
      stored === "general" ||
      stored === "runtime" ||
      stored === "providers" ||
      stored === "integrations" ||
      stored === "factory" ||
      stored === "hermesVersions" ||
      stored === "secrets" ||
      stored === "templates"
    ) {
      setActiveTab(stored);
    }
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    window.localStorage.setItem(SETTINGS_TAB_STORAGE_KEY, activeTab);
  }, [activeTab]);

  const enabledProviders = useMemo(
    () => (providers ?? []).filter((provider) => provider.enabled),
    [providers],
  );
  const systemOperator = useMemo(
    () => (agents ?? []).find((agent) => agent.is_system_agent && agent.slug === "hq-operator") ?? null,
    [agents],
  );
  const integrationCatalog = useMemo(
    () => integrationPackages ?? [],
    [integrationPackages],
  );
  const selectedDraft = useMemo(
    () => (integrationDrafts ?? []).find((draft) => draft.id === selectedDraftId) ?? null,
    [integrationDrafts, selectedDraftId],
  );
  const { data: selectedDraftFile } = useIntegrationDraftFile(
    selectedDraftId,
    selectedDraftPath,
    Boolean(isAdmin && activeTab === "factory"),
  );

  useEffect(() => {
    if (!integrationDrafts?.length) {
      setSelectedDraftId(null);
      setSelectedDraftPath(null);
      return;
    }
    if (!selectedDraftId || !integrationDrafts.some((draft) => draft.id === selectedDraftId)) {
      setSelectedDraftId(integrationDrafts[0].id);
    }
  }, [integrationDrafts, selectedDraftId]);

  useEffect(() => {
    if (!selectedDraft) {
      setDraftMetaName("");
      setDraftMetaDescription("");
      setDraftMetaVersion("0.1.0");
      setDraftNotes("");
      return;
    }
    setDraftMetaName(selectedDraft.name);
    setDraftMetaDescription(selectedDraft.description);
    setDraftMetaVersion(selectedDraft.version);
    setDraftNotes(selectedDraft.notes ?? "");
    if (!selectedDraftPath || !selectedDraft.files.some((file) => file.path === selectedDraftPath)) {
      setSelectedDraftPath(selectedDraft.files[0]?.path ?? null);
    }
  }, [selectedDraft, selectedDraftPath]);

  useEffect(() => {
    if (selectedDraftFile) {
      setDraftEditorContent(selectedDraftFile.content);
    } else if (!selectedDraftPath) {
      setDraftEditorContent("");
    }
  }, [selectedDraftFile, selectedDraftPath]);

  const selectedDefaultProvider = useMemo(
    () => enabledProviders.find((provider) => provider.slug === selectedDefaultProviderSlug) ?? null,
    [enabledProviders, selectedDefaultProviderSlug],
  );

  const settingsTabs: Array<{ id: SettingsTab; label: string; copy: string }> = [
    { id: "general", label: t("settings.tabGeneral"), copy: t("settings.tabGeneralCopy") },
    { id: "runtime", label: t("settings.tabRuntime"), copy: t("settings.tabRuntimeCopy") },
    { id: "providers", label: t("settings.tabProviders"), copy: t("settings.tabProvidersCopy") },
    { id: "integrations", label: t("settings.tabIntegrations"), copy: t("settings.tabIntegrationsCopy") },
    { id: "factory", label: t("settings.tabFactory"), copy: t("settings.tabFactoryCopy") },
    { id: "hermesVersions", label: t("settings.tabHermesVersions"), copy: t("settings.tabHermesVersionsCopy") },
    { id: "secrets", label: t("settings.tabSecrets"), copy: t("settings.tabSecretsCopy") },
    { id: "templates", label: t("settings.tabTemplates"), copy: t("settings.tabTemplatesCopy") },
  ];
  const activeTabMeta = settingsTabs.find((tab) => tab.id === activeTab) ?? settingsTabs[0];

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
      default_hermes_version: defaultHermesVersion === "bundled" ? null : defaultHermesVersion,
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

  async function submitIntegrationDraft(event: FormEvent) {
    event.preventDefault();
    const created = await createIntegrationDraft.mutateAsync({
      slug: draftSlug,
      name: draftName,
      description: draftDescription,
      template: draftTemplate,
      version: "0.1.0",
    });
    setDraftSlug("");
    setDraftName("");
    setDraftDescription("");
    setDraftTemplate("rest-api");
    setSelectedDraftId(created.id);
    setSelectedDraftPath(created.files[0]?.path ?? null);
  }

  async function saveDraftMetadata() {
    if (!selectedDraftId) {
      return;
    }
    await updateIntegrationDraft.mutateAsync({
      draftId: selectedDraftId,
      name: draftMetaName,
      description: draftMetaDescription,
      version: draftMetaVersion,
      notes: draftNotes,
    });
  }

  async function saveDraftFile(path: string) {
    if (!selectedDraftId) {
      return;
    }
    await saveIntegrationDraftFile.mutateAsync({
      draftId: selectedDraftId,
      path,
      content: draftEditorContent,
    });
  }

  async function createOrReplaceDraftFile() {
    if (!selectedDraftId || !newDraftFilePath.trim()) {
      return;
    }
    await saveIntegrationDraftFile.mutateAsync({
      draftId: selectedDraftId,
      path: newDraftFilePath.trim(),
      content: "",
    });
    setSelectedDraftPath(newDraftFilePath.trim());
    setNewDraftFilePath("");
  }

  async function removeSelectedDraftFile() {
    if (!selectedDraftId || !selectedDraftPath) {
      return;
    }
    await deleteIntegrationDraftFile.mutateAsync({ draftId: selectedDraftId, path: selectedDraftPath });
    setSelectedDraftPath(null);
  }

  async function runDraftValidation() {
    if (!selectedDraftId) {
      return;
    }
    await validateIntegrationDraft.mutateAsync(selectedDraftId);
  }

  async function publishSelectedDraft() {
    if (!selectedDraftId) {
      return;
    }
    await publishIntegrationDraft.mutateAsync(selectedDraftId);
  }

  async function removeSelectedDraft() {
    if (!selectedDraftId) {
      return;
    }
    await deleteIntegrationDraft.mutateAsync(selectedDraftId);
    setSelectedDraftId(null);
    setSelectedDraftPath(null);
  }

  async function submitHermesVersionCatalog(event: FormEvent) {
    event.preventDefault();
    await createHermesVersion.mutateAsync({
      version: newHermesVersion.trim(),
      release_tag: newHermesReleaseTag.trim() || null,
      description: newHermesDescription.trim() || null,
    });
    setNewHermesVersion("");
    setNewHermesReleaseTag("");
    setNewHermesDescription("");
  }

  async function saveHermesVersion(version: string) {
    const draft = hermesVersionDrafts[version];
    if (!draft) {
      return;
    }
    await updateHermesVersion.mutateAsync({
      version,
      payload: {
        release_tag: draft.release_tag.trim() || null,
        description: draft.description.trim() || null,
      },
    });
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

  const renderGeneralTab = () => (
    <section className="grid gap-6 xl:grid-cols-2">
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
            <select value={themeMode} onChange={(event) => setThemeMode(event.target.value as "dark" | "light" | "system" | "enterprise" | "sixmanager")}>
              <option value="dark">{t("common.dark")}</option>
              <option value="light">{t("common.light")}</option>
              <option value="system">{t("common.system")}</option>
              <option value="enterprise">{t("common.enterprise")}</option>
              <option value="sixmanager">{t("common.sixmanager")}</option>
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
    </section>
  );

  const renderRuntimeTab = () => (
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
          {systemOperator ? (
            <div className="mt-4 rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
              <p className="panel-label">{t("settings.hqOperatorReady")}</p>
              <p className="mt-2 text-sm text-[var(--text-display)]">
                {systemOperator.friendly_name || systemOperator.name}
              </p>
              <p className="mt-1 text-xs uppercase tracking-[0.12em] text-[var(--text-secondary)]">
                {systemOperator.runtime_profile} / {systemOperator.system_scope ?? "operator"}
              </p>
            </div>
          ) : null}
          <div className="mt-4 flex items-center gap-3">
            <button
              className="panel-button-primary"
              type="button"
              onClick={() => bootstrapSystemOperator.mutate()}
              disabled={bootstrapSystemOperator.isPending}
            >
              {systemOperator
                ? t("settings.hqOperatorRefresh")
                : bootstrapSystemOperator.isPending
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

  const renderHermesVersionsTab = () => (
    <section className="grid gap-6">
      <section className="panel-frame p-6">
        <p className="panel-label">Hermes Agent</p>
        <h2 className="mt-2 text-2xl text-[var(--text-display)]">Versions</h2>
        <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
          Install Hermes Agent releases once per instance and pin agents to a tested version.
        </p>
        <form className="mt-6 border border-[var(--border)] bg-[var(--surface-raised)] p-4" onSubmit={submitHermesVersionCatalog}>
          <p className="panel-label">Catalog</p>
          <div className="mt-4 grid gap-4 md:grid-cols-3">
            <label className="panel-field">
              <span className="panel-label">Version</span>
              <input value={newHermesVersion} onChange={(event) => setNewHermesVersion(event.target.value)} placeholder="0.10.0" />
            </label>
            <label className="panel-field">
              <span className="panel-label">Release tag</span>
              <input value={newHermesReleaseTag} onChange={(event) => setNewHermesReleaseTag(event.target.value)} placeholder="v2026.5.1" />
            </label>
            <label className="panel-field">
              <span className="panel-label">Description</span>
              <input value={newHermesDescription} onChange={(event) => setNewHermesDescription(event.target.value)} placeholder="Canary candidate" />
            </label>
          </div>
          <div className="mt-4 flex items-center gap-3">
            <button className="panel-button-primary" type="submit" disabled={createHermesVersion.isPending}>
              {createHermesVersion.isPending ? "Adding..." : "Add to catalog"}
            </button>
            <p className="panel-inline-status">New versions become installable without changing backend code.</p>
          </div>
        </form>
        <div className="mt-6 space-y-4">
          {(hermesVersions ?? []).map((version) => (
            <article key={version.version} className="border border-[var(--border)] bg-[var(--surface-raised)] p-4">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <p className="panel-label">{version.source}</p>
                  <h3 className="mt-2 text-lg text-[var(--text-display)]">
                    {version.version === "bundled" ? "Bundled runtime" : version.version}
                  </h3>
                  <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">
                    {version.description ?? "Hermes Agent runtime"}
                  </p>
                  <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-disabled)]">
                    {version.release_tag ?? (version.detected_version ? `detected ${version.detected_version}` : "no release tag")}
                  </p>
                </div>
                <div className="text-right">
                  <p className="panel-label">{version.installed ? "installed" : "available"}</p>
                  {version.is_default ? (
                    <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--accent)]">default</p>
                  ) : null}
                  {version.in_use_by_agents ? (
                    <p className="mt-2 text-xs uppercase tracking-[0.1em] text-[var(--text-secondary)]">
                      pinned by {version.in_use_by_agents}
                    </p>
                  ) : null}
                </div>
              </div>
              {version.version !== "bundled" ? (
                <div className="mt-4 grid gap-4 md:grid-cols-2">
                  <label className="panel-field">
                    <span className="panel-label">Release tag</span>
                    <input
                      value={hermesVersionDrafts[version.version]?.release_tag ?? ""}
                      onChange={(event) =>
                        setHermesVersionDrafts((current) => ({
                          ...current,
                          [version.version]: {
                            ...(current[version.version] ?? { release_tag: "", description: "" }),
                            release_tag: event.target.value,
                          },
                        }))
                      }
                    />
                  </label>
                  <label className="panel-field">
                    <span className="panel-label">Description</span>
                    <input
                      value={hermesVersionDrafts[version.version]?.description ?? ""}
                      onChange={(event) =>
                        setHermesVersionDrafts((current) => ({
                          ...current,
                          [version.version]: {
                            ...(current[version.version] ?? { release_tag: "", description: "" }),
                            description: event.target.value,
                          },
                        }))
                      }
                    />
                  </label>
                </div>
              ) : null}
              <div className="mt-4 flex flex-wrap gap-3">
                {version.version !== "bundled" ? (
                  <button
                    type="button"
                    className="panel-button-secondary"
                    disabled={updateHermesVersion.isPending}
                    onClick={() => void saveHermesVersion(version.version)}
                  >
                    {updateHermesVersion.isPending ? "Saving..." : "Save metadata"}
                  </button>
                ) : null}
                {version.version !== "bundled" && !version.installed ? (
                  <button
                    type="button"
                    className="panel-button-secondary"
                    disabled={installHermesVersion.isPending}
                    onClick={() => void installHermesVersion.mutateAsync(version.version)}
                  >
                    {installHermesVersion.isPending ? "Installing..." : "Install"}
                  </button>
                ) : null}
                {version.version !== "bundled" && version.installed ? (
                  <button
                    type="button"
                    className="panel-button-secondary"
                    disabled={uninstallHermesVersion.isPending || version.is_default || version.in_use_by_agents > 0}
                    onClick={() => void uninstallHermesVersion.mutateAsync(version.version)}
                  >
                    {uninstallHermesVersion.isPending ? "Removing..." : "Uninstall"}
                  </button>
                ) : null}
                {version.version !== "bundled" && !version.installed ? (
                  <button
                    type="button"
                    className="panel-button-secondary"
                    disabled={deleteHermesVersionCatalogEntry.isPending || version.is_default || version.in_use_by_agents > 0}
                    onClick={() => void deleteHermesVersionCatalogEntry.mutateAsync(version.version)}
                  >
                    {deleteHermesVersionCatalogEntry.isPending ? "Deleting..." : "Delete catalog entry"}
                  </button>
                ) : null}
              </div>
            </article>
          ))}
        </div>
      </section>
    </section>
  );

  const renderSecretsTab = () => (
    <section className="grid gap-6 xl:grid-cols-2">
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
    </section>
  );

  const renderTemplatesTab = () => (
    <section className="grid gap-6 xl:grid-cols-2">
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
    </section>
  );

  const renderIntegrationsTab = () => (
    <section className="grid gap-6">
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
  );

  const renderFactoryTab = () => (
    <section className="grid gap-6 xl:grid-cols-[0.68fr_1.32fr]">
      <div className="grid gap-6">
        <form className="panel-frame p-6" onSubmit={submitIntegrationDraft}>
          <p className="panel-label">{t("settings.integrationFactory")}</p>
          <h2 className="mt-2 text-2xl text-[var(--text-display)]">{t("settings.createIntegrationDraft")}</h2>
          <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
            {t("settings.integrationFactoryCreateCopy")}
          </p>
          <div className="mt-6 space-y-4">
            <label className="panel-field">
              <span className="panel-label">{t("settings.integrationDraftSlug")}</span>
              <input value={draftSlug} onChange={(event) => setDraftSlug(event.target.value)} placeholder="customer-api" />
            </label>
            <label className="panel-field">
              <span className="panel-label">{t("settings.integrationDraftName")}</span>
              <input value={draftName} onChange={(event) => setDraftName(event.target.value)} placeholder="Customer API" />
            </label>
            <label className="panel-field">
              <span className="panel-label">{t("settings.integrationDraftDescription")}</span>
              <textarea rows={4} value={draftDescription} onChange={(event) => setDraftDescription(event.target.value)} />
            </label>
            <label className="panel-field">
              <span className="panel-label">{t("settings.integrationDraftTemplate")}</span>
              <select value={draftTemplate} onChange={(event) => setDraftTemplate(event.target.value as "rest-api" | "empty")}>
                <option value="rest-api">{t("settings.integrationDraftTemplateRestApi")}</option>
                <option value="empty">{t("settings.integrationDraftTemplateEmpty")}</option>
              </select>
            </label>
            <button className="panel-button-primary w-full" type="submit" disabled={createIntegrationDraft.isPending}>
              {createIntegrationDraft.isPending ? t("common.loading") : t("settings.createIntegrationDraft")}
            </button>
          </div>
        </form>

        <div className="panel-frame p-6">
          <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
            <div>
              <p className="panel-label">{t("settings.integrationDrafts")}</p>
              <h2 className="mt-2 text-2xl text-[var(--text-display)]">{t("settings.integrationFactory")}</h2>
            </div>
            <p className="panel-label">{t("settings.integrationDraftCount", { count: integrationDrafts?.length ?? 0 })}</p>
          </div>
          <div className="mt-4 space-y-3">
            {(integrationDrafts ?? []).length ? (
              (integrationDrafts ?? []).map((draft) => (
                <button
                  key={draft.id}
                  type="button"
                  onClick={() => {
                    setSelectedDraftId(draft.id);
                    setSelectedDraftPath(draft.files[0]?.path ?? null);
                  }}
                  className={`w-full border p-4 text-left ${
                    draft.id === selectedDraftId
                      ? "rounded-2xl border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_10%,transparent)]"
                      : "rounded-2xl border-[var(--border)] bg-[var(--surface-raised)]"
                  }`}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="panel-label">{draft.slug}</p>
                      <p className="mt-2 text-sm text-[var(--text-display)]">{draft.name}</p>
                    </div>
                    <span className="rounded-full border border-[var(--border)] px-3 py-1 text-xs uppercase tracking-[0.1em] text-[var(--text-secondary)]">
                      {draft.status}
                    </span>
                  </div>
                  <p className="mt-2 text-sm text-[var(--text-secondary)]">{draft.description || t("settings.integrationDraftNoDescription")}</p>
                </button>
              ))
            ) : (
              <p className="panel-inline-status">{t("settings.integrationDraftsEmpty")}</p>
            )}
          </div>
        </div>
      </div>

      <div className="panel-frame p-6">
        {selectedDraft ? (
          <div className="grid gap-6">
            <div className="flex flex-wrap items-start justify-between gap-4 border-b border-[var(--border)] pb-4">
              <div>
                <p className="panel-label">{selectedDraft.slug}</p>
                <h2 className="mt-2 text-2xl text-[var(--text-display)]">{selectedDraft.name}</h2>
                <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                  {t("settings.integrationFactoryDetailCopy")}
                </p>
              </div>
              <div className="flex flex-wrap gap-3">
                <button type="button" className="panel-button-secondary" onClick={() => void runDraftValidation()} disabled={validateIntegrationDraft.isPending}>
                  {validateIntegrationDraft.isPending ? t("common.loading") : t("settings.validateIntegrationDraft")}
                </button>
                <button type="button" className="panel-button-primary" onClick={() => void publishSelectedDraft()} disabled={publishIntegrationDraft.isPending}>
                  {publishIntegrationDraft.isPending ? t("common.loading") : t("settings.publishIntegrationDraft")}
                </button>
                <button type="button" className="panel-button-secondary" onClick={() => void removeSelectedDraft()} disabled={deleteIntegrationDraft.isPending}>
                  {t("settings.deleteIntegrationDraft")}
                </button>
              </div>
            </div>

            <div className="grid gap-6 xl:grid-cols-[0.5fr_0.5fr]">
              <div className="space-y-4">
                <label className="panel-field">
                  <span className="panel-label">{t("settings.integrationDraftName")}</span>
                  <input value={draftMetaName} onChange={(event) => setDraftMetaName(event.target.value)} />
                </label>
                <label className="panel-field">
                  <span className="panel-label">{t("settings.integrationDraftDescription")}</span>
                  <textarea rows={4} value={draftMetaDescription} onChange={(event) => setDraftMetaDescription(event.target.value)} />
                </label>
                <label className="panel-field">
                  <span className="panel-label">{t("settings.integrationDraftVersion")}</span>
                  <input value={draftMetaVersion} onChange={(event) => setDraftMetaVersion(event.target.value)} />
                </label>
                <label className="panel-field">
                  <span className="panel-label">{t("settings.integrationDraftNotes")}</span>
                  <textarea rows={4} value={draftNotes} onChange={(event) => setDraftNotes(event.target.value)} />
                </label>
                <button type="button" className="panel-button-secondary w-full" onClick={() => void saveDraftMetadata()} disabled={updateIntegrationDraft.isPending}>
                  {updateIntegrationDraft.isPending ? t("common.loading") : t("settings.saveIntegrationDraftMeta")}
                </button>
              </div>
              <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
                <p className="panel-label">{t("settings.integrationDraftStatus")}</p>
                <div className="mt-4 space-y-2 text-sm text-[var(--text-secondary)]">
                  <p>{t("settings.integrationDraftTemplateLabel", { value: selectedDraft.template })}</p>
                  <p>{t("settings.integrationDraftPluginSlug", { value: selectedDraft.plugin_slug ?? t("agent.none") })}</p>
                  <p>{t("settings.integrationDraftSkillIdentifier", { value: selectedDraft.skill_identifier ?? t("agent.none") })}</p>
                  <p>{t("settings.integrationDraftProfiles", { value: selectedDraft.supported_profiles.join(", ") || t("agent.none") })}</p>
                  <p>{t("settings.integrationDraftPublishedPackage", { value: selectedDraft.published_package_slug ?? t("agent.none") })}</p>
                </div>
                {selectedDraft.last_validation ? (
                  <div className="mt-4 border-t border-[var(--border)] pt-4">
                    <p className="panel-label">{t("settings.integrationDraftValidation")}</p>
                    <div className="mt-3 space-y-2">
                      {selectedDraft.last_validation.checks.map((check, index) => (
                        <div key={`${check.code}-${index}`} className="rounded-xl border border-[var(--border)] px-3 py-2 text-sm text-[var(--text-secondary)]">
                          <p className="font-mono text-xs uppercase tracking-[0.08em] text-[var(--text-disabled)]">{check.level} / {check.code}</p>
                          <p className="mt-1">{check.message}</p>
                          {check.path ? <p className="mt-1 font-mono text-xs text-[var(--text-disabled)]">{check.path}</p> : null}
                        </div>
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            </div>

            <div className="grid gap-6 xl:grid-cols-[0.36fr_0.64fr]">
              <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
                <div className="flex items-center justify-between gap-3">
                  <p className="panel-label">{t("settings.integrationDraftFiles")}</p>
                  <p className="text-xs uppercase tracking-[0.08em] text-[var(--text-disabled)]">{selectedDraft.files.length}</p>
                </div>
                <div className="mt-4 space-y-2">
                  {selectedDraft.files.map((file) => (
                    <button
                      key={file.path}
                      type="button"
                      onClick={() => setSelectedDraftPath(file.path)}
                      className={`w-full rounded-xl border px-3 py-2 text-left text-sm ${
                        file.path === selectedDraftPath
                          ? "border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_10%,transparent)] text-[var(--text-display)]"
                          : "border-[var(--border)] bg-[transparent] text-[var(--text-secondary)]"
                      }`}
                    >
                      <p className="font-mono">{file.path}</p>
                      <p className="mt-1 text-xs uppercase tracking-[0.08em] text-[var(--text-disabled)]">{file.size} bytes</p>
                    </button>
                  ))}
                </div>
                <div className="mt-4 border-t border-[var(--border)] pt-4">
                  <label className="panel-field">
                    <span className="panel-label">{t("settings.integrationDraftNewFile")}</span>
                    <input value={newDraftFilePath} onChange={(event) => setNewDraftFilePath(event.target.value)} placeholder="plugin/helpers.py" />
                  </label>
                  <button type="button" className="panel-button-secondary mt-3 w-full" onClick={() => void createOrReplaceDraftFile()} disabled={saveIntegrationDraftFile.isPending}>
                    {t("settings.integrationDraftCreateFile")}
                  </button>
                </div>
              </div>

              <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p className="panel-label">{t("settings.integrationDraftEditor")}</p>
                    <p className="mt-1 font-mono text-xs text-[var(--text-disabled)]">{selectedDraftPath ?? t("settings.integrationDraftSelectFile")}</p>
                  </div>
                  <div className="flex gap-3">
                    <button type="button" className="panel-button-secondary" onClick={() => selectedDraftPath ? void saveDraftFile(selectedDraftPath) : undefined} disabled={!selectedDraftPath || saveIntegrationDraftFile.isPending}>
                      {saveIntegrationDraftFile.isPending ? t("common.loading") : t("settings.saveIntegrationDraftFile")}
                    </button>
                    <button type="button" className="panel-button-secondary" onClick={() => void removeSelectedDraftFile()} disabled={!selectedDraftPath || deleteIntegrationDraftFile.isPending}>
                      {t("settings.deleteIntegrationDraftFile")}
                    </button>
                  </div>
                </div>
                <textarea
                  className="mt-4 min-h-[32rem] font-mono text-sm"
                  value={draftEditorContent}
                  onChange={(event) => setDraftEditorContent(event.target.value)}
                  placeholder={t("settings.integrationDraftEditorPlaceholder")}
                  disabled={!selectedDraftPath}
                />
              </div>
            </div>
          </div>
        ) : (
          <p className="panel-inline-status">{t("settings.integrationDraftSelectPrompt")}</p>
        )}
      </div>
    </section>
  );

  const renderProvidersTab = () => (
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
  );

  return (
    <div className="settings-page grid gap-6">
      <section className="settings-shell panel-frame p-6">
        <div className="flex flex-col gap-4 border-b border-[var(--border)] pb-5">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <p className="panel-label">{t("settings.settings")}</p>
              <h2 className="mt-2 text-3xl text-[var(--text-display)]">{activeTabMeta.label}</h2>
            </div>
            <p className="max-w-[34rem] text-sm leading-6 text-[var(--text-secondary)]">
              {activeTabMeta.copy}
            </p>
          </div>
          <div className="settings-tab-strip flex flex-wrap gap-2">
            {settingsTabs.map((tab) => {
              const isActive = tab.id === activeTab;
              return (
                <button
                  key={tab.id}
                  type="button"
                  onClick={() => setActiveTab(tab.id)}
                  className={`settings-tab-button ${
                    isActive
                      ? "is-active rounded-full border border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_16%,transparent)] px-4 py-2 text-sm text-[var(--text-display)]"
                      : "rounded-full border border-[var(--border)] bg-[var(--surface-raised)] px-4 py-2 text-sm text-[var(--text-secondary)] transition hover:text-[var(--text-display)]"
                  }`}
                >
                  {tab.label}
                </button>
              );
            })}
          </div>
        </div>
      </section>

      <div className="settings-content">
        {activeTab === "general" ? renderGeneralTab() : null}
        {activeTab === "runtime" ? renderRuntimeTab() : null}
        {activeTab === "providers" ? renderProvidersTab() : null}
        {activeTab === "integrations" ? renderIntegrationsTab() : null}
        {activeTab === "factory" ? renderFactoryTab() : null}
        {activeTab === "hermesVersions" ? renderHermesVersionsTab() : null}
        {activeTab === "secrets" ? renderSecretsTab() : null}
        {activeTab === "templates" ? renderTemplatesTab() : null}
      </div>
    </div>
  );
}

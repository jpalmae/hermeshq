import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { useAgents, useBootstrapSystemOperator } from "../api/agents";
import {
  useCreateInstanceBackup,
  useRestoreInstanceBackup,
  useRestoreInstanceBackupJob,
  useValidateInstanceBackup,
} from "../api/backup";
import {
  useCreateHermesVersion,
  useHermesVersions,
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
import {
  useCreateMcpAccessToken,
  useMcpAccessTokens,
  useRevokeMcpAccessToken,
  useUpdateMcpAccessToken,
} from "../api/mcpAccess";
import { useProviders, useUpdateProvider } from "../api/providers";
import { useRuntimeCapabilityOverview } from "../api/runtimeProfiles";
import { useCreateSecret, useSecrets } from "../api/secrets";
import {
  useDeleteBrandAsset,
  useDeleteTuiSkin,
  useSettings,
  useUpdateSettings,
  useUploadBrandAsset,
  useUploadTuiSkin,
} from "../api/settings";
import { useCreateTemplate, useTemplates } from "../api/templates";
import { useI18n } from "../lib/i18n";
import { useSessionStore } from "../stores/sessionStore";

/* ─── Lazy-loaded tab components ─── */
const GeneralTab = lazy(() => import("../components/settings/GeneralTab").then((m) => ({ default: m.default })));
const RuntimeTab = lazy(() => import("../components/settings/RuntimeTab").then((m) => ({ default: m.default })));
const ProvidersTab = lazy(() => import("../components/settings/ProvidersTab").then((m) => ({ default: m.ProvidersTab })));
const IntegrationsTab = lazy(() => import("../components/settings/IntegrationsTab").then((m) => ({ default: m.default })));
const FactoryTab = lazy(() => import("../components/settings/FactoryTab").then((m) => ({ default: m.default })));
const ExternalAccessTab = lazy(() => import("../components/settings/ExternalAccessTab").then((m) => ({ default: m.default })));
const HermesVersionsTab = lazy(() => import("../components/settings/HermesVersionsTab").then((m) => ({ default: m.default })));
const SecretsTab = lazy(() => import("../components/settings/SecretsTab").then((m) => ({ default: m.default })));
const TemplatesTab = lazy(() => import("../components/settings/TemplatesTab").then((m) => ({ default: m.default })));
const AuthenticationTab = lazy(() => import("../components/settings/AuthenticationTab").then((m) => ({ default: m.AuthenticationTab })));

type SettingsTab = "general" | "runtime" | "providers" | "integrations" | "factory" | "externalAccess" | "hermesVersions" | "secrets" | "templates" | "authentication";

const SETTINGS_TAB_STORAGE_KEY = "hermeshq.settings.activeTab";

const ALL_TABS: SettingsTab[] = [
  "general", "runtime", "providers", "integrations",
  "factory", "externalAccess", "hermesVersions", "secrets", "templates", "authentication",
];

function LoadingFallback() {
  return <div className="panel-frame p-6 text-sm text-[var(--text-secondary)]">Loading…</div>;
}

export function SettingsPage() {
  const queryClient = useQueryClient();
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";
  const { t } = useI18n();

  /* ─── Data hooks ─── */
  const { data: agents } = useAgents();
  const bootstrapSystemOperator = useBootstrapSystemOperator();
  const { data: secrets } = useSecrets(isAdmin);
  const { data: providers } = useProviders(Boolean(currentUser));
  const { data: hermesVersions } = useHermesVersions(isAdmin);
  const { data: runtimeCapabilityOverview } = useRuntimeCapabilityOverview(Boolean(currentUser));
  const { data: integrationPackages } = useIntegrationPackages(isAdmin);
  const { data: integrationDrafts } = useIntegrationDrafts(isAdmin);
  const { data: mcpAccessTokens } = useMcpAccessTokens(isAdmin);
  const { data: templates } = useTemplates(isAdmin);
  const { data: settings } = useSettings(isAdmin);

  /* ─── Mutation hooks ─── */
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
  const createInstanceBackup = useCreateInstanceBackup();
  const validateInstanceBackup = useValidateInstanceBackup();
  const restoreInstanceBackup = useRestoreInstanceBackup();
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
  const createMcpAccessToken = useCreateMcpAccessToken();
  const updateMcpAccessToken = useUpdateMcpAccessToken();
  const revokeMcpAccessToken = useRevokeMcpAccessToken();

  /* ─── Tab state ─── */
  const [activeTab, setActiveTab] = useState<SettingsTab>("general");

  /* ─── Restore job polling ─── */
  const [activeRestoreJobId, setActiveRestoreJobId] = useState<string | null>(null);
  const { data: restoreJob } = useRestoreInstanceBackupJob(activeRestoreJobId);

  /* ─── Integration draft file ─── */
  const [selectedDraftId, setSelectedDraftId] = useState<string | null>(null);
  const [selectedDraftPath, setSelectedDraftPath] = useState<string | null>(null);
  const { data: selectedDraftFile } = useIntegrationDraftFile(
    selectedDraftId, selectedDraftPath, Boolean(isAdmin && activeTab === "factory"),
  );

  /* ─── Tab persistence ─── */
  useEffect(() => {
    if (typeof window === "undefined") return;
    const stored = window.localStorage.getItem(SETTINGS_TAB_STORAGE_KEY);
    if (stored && ALL_TABS.includes(stored as SettingsTab)) setActiveTab(stored as SettingsTab);
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(SETTINGS_TAB_STORAGE_KEY, activeTab);
  }, [activeTab]);

  /* ─── Tab metadata ─── */
  const settingsTabs: Array<{ id: SettingsTab; label: string; copy: string }> = [
    { id: "general", label: t("settings.tabGeneral"), copy: t("settings.tabGeneralCopy") },
    { id: "runtime", label: t("settings.tabRuntime"), copy: t("settings.tabRuntimeCopy") },
    { id: "providers", label: t("settings.tabProviders"), copy: t("settings.tabProvidersCopy") },
    { id: "integrations", label: t("settings.tabIntegrations"), copy: t("settings.tabIntegrationsCopy") },
    { id: "factory", label: t("settings.tabFactory"), copy: t("settings.tabFactoryCopy") },
    { id: "externalAccess", label: "External access", copy: "Expose selected HermesHQ agents to enterprise MCP clients such as Claude Code, Codex and Claude Desktop." },
    { id: "hermesVersions", label: t("settings.tabHermesVersions"), copy: t("settings.tabHermesVersionsCopy") },
    { id: "secrets", label: t("settings.tabSecrets"), copy: t("settings.tabSecretsCopy") },
    { id: "templates", label: t("settings.tabTemplates"), copy: t("settings.tabTemplatesCopy") },
    { id: "authentication", label: "Authentication", copy: "Configure enterprise OIDC providers (Google, Microsoft 365) for single sign-on." },
  ];

  const activeTabMeta = settingsTabs.find((tab) => tab.id === activeTab) ?? settingsTabs[0];

  /* ─── Guard: admin only ─── */
  if (currentUser && !isAdmin) {
    return (
      <section className="panel-frame p-6">
        <p className="panel-label">{t("settings.settings")}</p>
        <h2 className="mt-2 text-3xl text-[var(--text-display)]">{t("settings.adminRequired")}</h2>
        <p className="mt-4 max-w-[42rem] text-sm leading-6 text-[var(--text-secondary)]">{t("settings.adminCopy")}</p>
      </section>
    );
  }

  return (
    <div className="settings-page grid gap-6">
      <section className="settings-shell panel-frame p-6">
        <div className="flex flex-col gap-4 border-b border-[var(--border)] pb-5">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <p className="panel-label">{t("settings.settings")}</p>
              <h2 className="mt-2 text-3xl text-[var(--text-display)]">{activeTabMeta.label}</h2>
            </div>
            <p className="max-w-[34rem] text-sm leading-6 text-[var(--text-secondary)]">{activeTabMeta.copy}</p>
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
        <Suspense fallback={<LoadingFallback />}>
          {activeTab === "general" && (
            <GeneralTab
              settings={settings}
              updateSettings={updateSettings}
              uploadLogo={uploadLogo}
              uploadFavicon={uploadFavicon}
              deleteLogo={deleteLogo}
              deleteFavicon={deleteFavicon}
              uploadTuiSkin={uploadTuiSkin}
              deleteTuiSkin={deleteTuiSkin}
              createInstanceBackup={createInstanceBackup}
              validateInstanceBackup={validateInstanceBackup}
              restoreInstanceBackup={restoreInstanceBackup}
              restoreJob={restoreJob}
              queryClient={queryClient}
            />
          )}
          {activeTab === "runtime" && (
            <RuntimeTab />
          )}
          {activeTab === "providers" && (
            <ProvidersTab />
          )}
          {activeTab === "integrations" && (
            <IntegrationsTab />
          )}
          {activeTab === "factory" && (
            <FactoryTab
              integrationDrafts={integrationDrafts}
              createIntegrationDraft={createIntegrationDraft}
              updateIntegrationDraft={updateIntegrationDraft}
              deleteIntegrationDraft={deleteIntegrationDraft}
              saveIntegrationDraftFile={saveIntegrationDraftFile}
              deleteIntegrationDraftFile={deleteIntegrationDraftFile}
              validateIntegrationDraft={validateIntegrationDraft}
              publishIntegrationDraft={publishIntegrationDraft}
              integrationDraftFiles={selectedDraftFile}
              queryClient={queryClient}
            />
          )}
          {activeTab === "externalAccess" && (
            <ExternalAccessTab
              agents={agents}
              mcpAccessTokens={mcpAccessTokens}
              createMcpAccessToken={createMcpAccessToken}
              updateMcpAccessToken={updateMcpAccessToken}
              revokeMcpAccessToken={revokeMcpAccessToken}
            />
          )}
          {activeTab === "hermesVersions" && (
            <HermesVersionsTab
              hermesVersions={hermesVersions}
            />
          )}
          {activeTab === "secrets" && (
            <SecretsTab />
          )}
          {activeTab === "templates" && (
            <TemplatesTab />
          )}
          {activeTab === "authentication" && isAdmin && (
            <AuthenticationTab />
          )}
        </Suspense>
      </div>
    </div>
  );
}

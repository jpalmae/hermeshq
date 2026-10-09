import { lazy, Suspense, useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { useAgents } from "../../api/agents";
import { useCreateInstanceBackup, useRestoreInstanceBackup, useValidateInstanceBackup } from "../../api/backup";
import { useHermesVersions } from "../../api/hermesVersions";
import {
  useCreateIntegrationDraft,
  useIntegrationDrafts,
  useDeleteIntegrationDraft,
  useDeleteIntegrationDraftFile,
  usePublishIntegrationDraft,
  useSaveIntegrationDraftFile,
  useUpdateIntegrationDraft,
  useValidateIntegrationDraft,
} from "../../api/integrationFactory";
import {
  useCreateMcpAccessToken,
  useMcpAccessTokens,
  useRevokeMcpAccessToken,
  useUpdateMcpAccessToken,
} from "../../api/mcpAccess";
import {
  useCreatePublicChatKey,
  useDeletePublicChatKey,
  usePermanentlyDeletePublicChatKey,
  usePublicChatKeys,
  useUpdatePublicChatKey,
} from "../../api/publicChatKeys";
import {
  useDeleteBrandAsset,
  useDeleteTuiSkin,
  useSettings,
  useUpdateSettings,
  useUploadBrandAsset,
  useUploadTuiSkin,
} from "../../api/settings";
import { useSessionStore } from "../../stores/sessionStore";
import { useI18n } from "../../lib/i18n";

const GeneralTab = lazy(() => import("../../components/settings/GeneralTab"));
const RuntimeTab = lazy(() => import("../../components/settings/RuntimeTab").then((m) => ({ default: m.RuntimeTab })));
const ProvidersTab = lazy(() => import("../../components/settings/ProvidersTab").then((m) => ({ default: m.ProvidersTab })));
const IntegrationsTab = lazy(() => import("../../components/settings/IntegrationsTab"));
const FactoryTab = lazy(() => import("../../components/settings/FactoryTab"));
const ExternalAccessTab = lazy(() => import("../../components/settings/ExternalAccessTab"));
const HermesVersionsTab = lazy(() => import("../../components/settings/HermesVersionsTab"));
const SecretsTab = lazy(() => import("../../components/settings/SecretsTab").then((m) => ({ default: m.SecretsTab })));
const TemplatesTab = lazy(() => import("../../components/settings/TemplatesTab").then((m) => ({ default: m.TemplatesTab })));
const AuthenticationTab = lazy(() => import("../../components/settings/AuthenticationTab").then((m) => ({ default: m.AuthenticationTab })));
const EmailTab = lazy(() => import("../../components/settings/EmailTab").then((m) => ({ default: m.EmailTab })));
const ResourcesTab = lazy(() => import("../../components/settings/ResourcesTab"));
const M365Tab = lazy(() => import("../../components/settings/M365Tab"));
const PublicChatKeysTab = lazy(() => import("../../components/settings/PublicChatKeysTab"));
const PermissionPoliciesTab = lazy(() => import("../../components/settings/PermissionPoliciesTab").then((m) => ({ default: m.PermissionPoliciesTab })));

type SettingsTab =
  | "general" | "runtime" | "providers" | "integrations" | "factory"
  | "externalAccess" | "hermesVersions" | "secrets" | "templates"
  | "authentication" | "email" | "resources" | "m365" | "publicChatKeys" | "permissionPolicies";

const TAB_GROUPS: Array<{ groupKey: string; tabs: Array<{ id: SettingsTab; labelKey: string }> }> = [
  {
    groupKey: "v2.instanceGroup",
    tabs: [
      { id: "general", labelKey: "v2.general" },
      { id: "runtime", labelKey: "v2.runtimeTab" },
      { id: "resources", labelKey: "v2.resourcesTab" },
      { id: "hermesVersions", labelKey: "v2.hermesVersionsTab" },
    ],
  },
  {
    groupKey: "v2.agents",
    tabs: [
      { id: "providers", labelKey: "v2.providersTab" },
      { id: "integrations", labelKey: "v2.integrations" },
      { id: "factory", labelKey: "v2.factoryTab" },
      { id: "templates", labelKey: "v2.templatesTab" },
      { id: "permissionPolicies", labelKey: "v2.permissionPolicies" },
    ],
  },
  {
    groupKey: "v2.accessGroup",
    tabs: [
      { id: "authentication", labelKey: "v2.authenticationTab" },
      { id: "email", labelKey: "v2.emailTab" },
      { id: "secrets", labelKey: "v2.secretsTab" },
      { id: "externalAccess", labelKey: "v2.externalAccessTab" },
      { id: "m365", labelKey: "v2.microsoft365Tab" },
      { id: "publicChatKeys", labelKey: "v2.publicChatTab" },
    ],
  },
];

const TAB_STORAGE_KEY = "v2.settings.activeTab";

function TabFallback() {
  return (
    <div style={{ padding: 20 }}>
      <div className="v2-skeleton" style={{ height: 28, width: 220, marginBottom: 12 }} />
      <div className="v2-skeleton" style={{ height: 200 }} />
    </div>
  );
}

export function V2SettingsPage() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";

  const { data: agents } = useAgents();
  const { data: settings } = useSettings(isAdmin);
  const { data: hermesVersions } = useHermesVersions(isAdmin);
  const { data: integrationDrafts } = useIntegrationDrafts(isAdmin);
  const { data: mcpAccessTokens } = useMcpAccessTokens(isAdmin);
  const { data: publicChatKeys } = usePublicChatKeys(isAdmin);

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
  const createPublicChatKey = useCreatePublicChatKey();
  const updatePublicChatKey = useUpdatePublicChatKey();
  const deletePublicChatKey = useDeletePublicChatKey();
  const permanentlyDeletePublicChatKey = usePermanentlyDeletePublicChatKey();

  const [activeTab, setActiveTab] = useState<SettingsTab>(() => {
    const stored = localStorage.getItem(TAB_STORAGE_KEY);
    return (stored as SettingsTab) || "general";
  });

  useEffect(() => {
    localStorage.setItem(TAB_STORAGE_KEY, activeTab);
  }, [activeTab]);

  if (!isAdmin) {
    return (
      <div className="v2-empty">
        <p className="v2-empty-title">{t("v2.adminAccessRequired")}</p>
        <p className="v2-empty-text">{t("v2.settingsAdminOnly")}</p>
      </div>
    );
  }

  return (
    <div>
      <div className="v2-page-header">
        <div>
          <h1 className="v2-page-title">{t("v2.settings")}</h1>
          <p className="v2-page-subtitle">{t("v2.instanceConfig")}</p>
        </div>
      </div>

      <div style={{ display: "flex", gap: 24, alignItems: "flex-start" }}>
        <nav style={{ width: 190, flexShrink: 0, position: "sticky", top: 80 }}>
          {TAB_GROUPS.map((group) => (
            <div key={group.groupKey} style={{ marginBottom: 20 }}>
              <div style={{ fontSize: 11, fontWeight: 620, textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--v2-text-muted)", padding: "0 10px", marginBottom: 6 }}>
                {t(group.groupKey)}
              </div>
              {group.tabs.map((tab) => (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  style={{
                    display: "block",
                    width: "100%",
                    textAlign: "left",
                    padding: "7px 10px",
                    borderRadius: 7,
                    border: "none",
                    background: activeTab === tab.id ? "var(--v2-accent-subtle)" : "transparent",
                    color: activeTab === tab.id ? "var(--v2-accent)" : "var(--v2-text-secondary)",
                    fontSize: 13,
                    fontWeight: activeTab === tab.id ? 620 : 500,
                    fontFamily: "var(--v2-font-sans)",
                    cursor: "pointer",
                    marginBottom: 1,
                  }}
                >
                  {t(tab.labelKey)}
                </button>
              ))}
            </div>
          ))}
        </nav>

        <div style={{ flex: 1, minWidth: 0 }}>
          <Suspense fallback={<TabFallback />}>
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
                queryClient={queryClient}
              />
            )}
            {activeTab === "runtime" && <RuntimeTab />}
            {activeTab === "providers" && <ProvidersTab />}
            {activeTab === "integrations" && <IntegrationsTab />}
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
            {activeTab === "hermesVersions" && <HermesVersionsTab hermesVersions={hermesVersions} />}
            {activeTab === "secrets" && <SecretsTab />}
            {activeTab === "templates" && <TemplatesTab />}
            {activeTab === "authentication" && <AuthenticationTab />}
            {activeTab === "email" && <EmailTab />}
            {activeTab === "resources" && <ResourcesTab />}
            {activeTab === "m365" && <M365Tab />}
            {activeTab === "publicChatKeys" && (
              <PublicChatKeysTab
                agents={agents}
                publicChatKeys={publicChatKeys}
                createPublicChatKey={createPublicChatKey}
                updatePublicChatKey={updatePublicChatKey}
                deletePublicChatKey={deletePublicChatKey}
                permanentlyDeletePublicChatKey={permanentlyDeletePublicChatKey}
              />
            )}
            {activeTab === "permissionPolicies" && <PermissionPoliciesTab />}
          </Suspense>
        </div>
      </div>
    </div>
  );
}

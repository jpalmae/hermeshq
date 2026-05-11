import { useInstallIntegrationPackage, useIntegrationPackages, useUninstallIntegrationPackage, useUploadIntegrationPackage } from "../../api/integrationPackages";
import { useI18n } from "../../lib/i18n";
import { useSessionStore } from "../../stores/sessionStore";

export function IntegrationsTab() {
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";
  const { t } = useI18n();
  const { data: integrationPackages } = useIntegrationPackages(isAdmin);
  const uploadIntegrationPackage = useUploadIntegrationPackage();
  const installIntegrationPackage = useInstallIntegrationPackage();
  const uninstallIntegrationPackage = useUninstallIntegrationPackage();

  const integrationCatalog = integrationPackages ?? [];

  async function onIntegrationPackageSelected(file: File | null) {
    if (!file) return;
    try {
      await uploadIntegrationPackage.mutateAsync(file);
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Integration package upload failed");
    }
  }

  return (
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
}

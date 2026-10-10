import { useState } from "react";

import { useAgents } from "../../api/agents";
import {
  useCloudBindings,
  useCloudPlatforms,
  useCloudTenants,
  useCreateCloudBinding,
  useCreateCloudPlatform,
  useCreateCloudTenant,
  useDeleteCloudBinding,
  useDeleteCloudPlatform,
  useDeleteCloudTenant,
} from "../../api/cloud";
import { useI18n } from "../../lib/i18n";
import { useSessionStore } from "../../stores/sessionStore";

export function CloudTab() {
  const currentUser = useSessionStore((state) => state.user);
  const { t } = useI18n();
  const isAdmin = currentUser?.role === "admin";
  const { data: platforms } = useCloudPlatforms(Boolean(currentUser));
  const { data: tenants } = useCloudTenants(Boolean(currentUser));
  const { data: bindings } = useCloudBindings(Boolean(currentUser));
  const { data: agents } = useAgents(Boolean(currentUser));
  const createPlatform = useCreateCloudPlatform();
  const createTenant = useCreateCloudTenant();
  const createBinding = useCreateCloudBinding();
  const deletePlatform = useDeleteCloudPlatform();
  const deleteTenant = useDeleteCloudTenant();
  const deleteBinding = useDeleteCloudBinding();

  const [platformDraft, setPlatformDraft] = useState({ name: "", api_url: "", insecure_tls: false });
  const [tenantDraft, setTenantDraft] = useState({ platform_id: "", tenant_ref: "", display_name: "", api_key: "" });
  const [bindingDraft, setBindingDraft] = useState({ tenant_id: "", agent_id: "", cloud_role: "viewer" });
  const [message, setMessage] = useState<string | null>(null);

  const tenantById = new Map((tenants ?? []).map((tenant) => [tenant.id, tenant]));
  const agentById = new Map((agents ?? []).map((agent) => [agent.id, agent]));

  async function savePlatform() {
    if (!platformDraft.name.trim() || !platformDraft.api_url.trim()) return;
    try {
      await createPlatform.mutateAsync({
        name: platformDraft.name.trim(),
        api_url: platformDraft.api_url.trim(),
        kind: "vergeos",
        insecure_tls: platformDraft.insecure_tls,
      });
      setPlatformDraft({ name: "", api_url: "", insecure_tls: false });
      setMessage(null);
    } catch (error) {
      setMessage(String((error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? error));
    }
  }

  async function saveTenant() {
    if (!tenantDraft.platform_id || !tenantDraft.tenant_ref.trim() || !tenantDraft.display_name.trim()) return;
    try {
      await createTenant.mutateAsync({
        platform_id: tenantDraft.platform_id,
        tenant_ref: tenantDraft.tenant_ref.trim(),
        display_name: tenantDraft.display_name.trim(),
        credential_ref: `cloud-tenant-${tenantDraft.tenant_ref.trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-")}`,
        api_key: tenantDraft.api_key || null,
      });
      setTenantDraft({ platform_id: tenantDraft.platform_id, tenant_ref: "", display_name: "", api_key: "" });
      setMessage(null);
    } catch (error) {
      setMessage(String((error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? error));
    }
  }

  async function saveBinding() {
    if (!bindingDraft.tenant_id || !bindingDraft.agent_id) return;
    try {
      await createBinding.mutateAsync(bindingDraft);
      setBindingDraft({ tenant_id: bindingDraft.tenant_id, agent_id: "", cloud_role: "viewer" });
      setMessage(null);
    } catch (error) {
      setMessage(String((error as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? error));
    }
  }

  return (
    <section className="panel-frame p-6">
      <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
        <div>
          <p className="panel-label">{t("cloud.platforms")}</p>
          <h2 className="mt-2 text-3xl text-[var(--text-display)]">{t("cloud.title")}</h2>
          <p className="mt-2 max-w-2xl text-sm text-[var(--text-secondary)]">{t("cloud.description")}</p>
        </div>
      </div>

      {message ? (
        <p className="mt-4 rounded border border-[var(--danger,var(--danger))] px-3 py-2 text-xs text-[var(--danger,var(--danger))]">
          {message}
        </p>
      ) : null}

      <div className="mt-6 space-y-8">
        <div>
          <h3 className="text-lg text-[var(--text-display)]">{t("cloud.platformsTitle")}</h3>
          <div className="mt-3 grid gap-3 xl:grid-cols-2">
            {(platforms ?? []).map((platform) => (
              <article key={platform.id} className="rounded border border-[var(--border)] p-4">
                <div className="flex items-start justify-between">
                  <div>
                    <p className="font-medium text-[var(--text-display)]">{platform.name}</p>
                    <p className="text-xs text-[var(--text-secondary)]">
                      {platform.kind} · {platform.api_url}
                      {platform.insecure_tls ? " · TLS inseguro" : ""}
                    </p>
                  </div>
                  {isAdmin ? (
                    <button
                      type="button"
                      className="panel-button-ghost text-xs"
                      onClick={() => void deletePlatform.mutateAsync(platform.id)}
                    >
                      {t("common.delete")}
                    </button>
                  ) : null}
                </div>
              </article>
            ))}
          </div>
          {isAdmin ? (
            <div className="mt-3 flex flex-wrap items-end gap-3">
              <label className="panel-field">
                <span className="panel-label">{t("cloud.name")}</span>
                <input className="panel-input" value={platformDraft.name} onChange={(e) => setPlatformDraft({ ...platformDraft, name: e.target.value })} />
              </label>
              <label className="panel-field flex-1">
                <span className="panel-label">API URL</span>
                <input className="panel-input" placeholder="https://verge.example" value={platformDraft.api_url} onChange={(e) => setPlatformDraft({ ...platformDraft, api_url: e.target.value })} />
              </label>
              <label className="panel-field">
                <span className="panel-label">TLS</span>
                <input type="checkbox" checked={platformDraft.insecure_tls} onChange={(e) => setPlatformDraft({ ...platformDraft, insecure_tls: e.target.checked })} />
              </label>
              <button type="button" className="panel-button-primary" onClick={() => void savePlatform()}>
                {t("cloud.addPlatform")}
              </button>
            </div>
          ) : null}
        </div>

        <div>
          <h3 className="text-lg text-[var(--text-display)]">{t("cloud.tenantsTitle")}</h3>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-[var(--border)] text-left text-xs text-[var(--text-secondary)]">
                  <th className="py-2">{t("cloud.tenant")}</th>
                  <th>Ref</th>
                  <th>Plataforma</th>
                  <th>Credential</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(tenants ?? []).map((tenant) => (
                  <tr key={tenant.id} className="border-b border-[var(--border)]/50">
                    <td className="py-2">{tenant.display_name}</td>
                    <td className="text-xs text-[var(--text-secondary)]">{tenant.tenant_ref}</td>
                    <td className="text-xs">{platforms?.find((p) => p.id === tenant.platform_id)?.name ?? tenant.platform_id}</td>
                    <td className="text-xs text-[var(--text-secondary)]">{tenant.credential_ref}</td>
                    <td className="text-right">
                      {isAdmin ? (
                        <button type="button" className="panel-button-ghost text-xs" onClick={() => void deleteTenant.mutateAsync(tenant.id)}>
                          {t("common.delete")}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {isAdmin ? (
            <div className="mt-3 flex flex-wrap items-end gap-3">
              <label className="panel-field">
                <span className="panel-label">{t("cloud.platform")}</span>
                <select className="panel-input" value={tenantDraft.platform_id} onChange={(e) => setTenantDraft({ ...tenantDraft, platform_id: e.target.value })}>
                  <option value="">—</option>
                  {(platforms ?? []).filter((p) => p.active).map((p) => (
                    <option key={p.id} value={p.id}>{p.name}</option>
                  ))}
                </select>
              </label>
              <label className="panel-field">
                <span className="panel-label">Ref</span>
                <input className="panel-input" value={tenantDraft.tenant_ref} onChange={(e) => setTenantDraft({ ...tenantDraft, tenant_ref: e.target.value })} />
              </label>
              <label className="panel-field">
                <span className="panel-label">{t("cloud.displayName")}</span>
                <input className="panel-input" value={tenantDraft.display_name} onChange={(e) => setTenantDraft({ ...tenantDraft, display_name: e.target.value })} />
              </label>
              <label className="panel-field flex-1">
                <span className="panel-label">API key (tenant)</span>
                <input className="panel-input" type="password" placeholder="se guarda cifrada" value={tenantDraft.api_key} onChange={(e) => setTenantDraft({ ...tenantDraft, api_key: e.target.value })} />
              </label>
              <button type="button" className="panel-button-primary" onClick={() => void saveTenant()}>
                {t("cloud.addTenant")}
              </button>
            </div>
          ) : null}
        </div>

        <div>
          <h3 className="text-lg text-[var(--text-display)]">{t("cloud.bindingsTitle")}</h3>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-[var(--border)] text-left text-xs text-[var(--text-secondary)]">
                  <th className="py-2">{t("cloud.agent")}</th>
                  <th>{t("cloud.tenant")}</th>
                  <th>Rol</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {(bindings ?? []).map((binding) => (
                  <tr key={binding.id} className="border-b border-[var(--border)]/50">
                    <td className="py-2">{agentById.get(binding.agent_id)?.friendly_name ?? binding.agent_id}</td>
                    <td>{tenantById.get(binding.tenant_id)?.display_name ?? binding.tenant_id}</td>
                    <td><span className="panel-pill">{binding.cloud_role}</span></td>
                    <td className="text-right">
                      {isAdmin ? (
                        <button type="button" className="panel-button-ghost text-xs" onClick={() => void deleteBinding.mutateAsync(binding.id)}>
                          {t("common.delete")}
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {isAdmin ? (
            <div className="mt-3 flex flex-wrap items-end gap-3">
              <label className="panel-field">
                <span className="panel-label">{t("cloud.tenant")}</span>
                <select className="panel-input" value={bindingDraft.tenant_id} onChange={(e) => setBindingDraft({ ...bindingDraft, tenant_id: e.target.value })}>
                  <option value="">—</option>
                  {(tenants ?? []).filter((tenant) => tenant.active).map((tenant) => (
                    <option key={tenant.id} value={tenant.id}>{tenant.display_name}</option>
                  ))}
                </select>
              </label>
              <label className="panel-field">
                <span className="panel-label">{t("cloud.agent")}</span>
                <select className="panel-input" value={bindingDraft.agent_id} onChange={(e) => setBindingDraft({ ...bindingDraft, agent_id: e.target.value })}>
                  <option value="">—</option>
                  {(agents ?? []).filter((a) => !a.is_archived).map((a) => (
                    <option key={a.id} value={a.id}>{a.friendly_name ?? a.name}</option>
                  ))}
                </select>
              </label>
              <label className="panel-field">
                <span className="panel-label">Rol</span>
                <select className="panel-input" value={bindingDraft.cloud_role} onChange={(e) => setBindingDraft({ ...bindingDraft, cloud_role: e.target.value })}>
                  <option value="viewer">viewer</option>
                  <option value="operator">operator</option>
                  <option value="admin">admin</option>
                </select>
              </label>
              <button type="button" className="panel-button-primary" onClick={() => void saveBinding()}>
                {t("cloud.addBinding")}
              </button>
            </div>
          ) : null}
        </div>
      </div>
    </section>
  );
}

export default CloudTab;

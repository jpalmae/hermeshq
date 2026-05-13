import { useState } from "react";
import {
  listOidcProviders,
  createOidcProvider,
  updateOidcProvider,
  deleteOidcProvider,
  type OidcProviderRead,
  type OidcProviderCreate,
} from "../../api/oidcProviders";
import { useI18n } from "../../lib/i18n";

const PRESET_PROVIDERS: Record<string, { discovery_url: string; scopes: string; icon_slug: string }> = {
  google: {
    discovery_url: "https://accounts.google.com",
    scopes: "openid profile email",
    icon_slug: "google",
  },
  microsoft: {
    discovery_url: "https://login.microsoftonline.com/common/v2.0",
    scopes: "openid profile email User.Read",
    icon_slug: "microsoft",
  },
};

function ProviderIcon({ slug }: { slug: string | null }) {
  const s = (slug || "").toLowerCase();
  if (s === "google") {
    return (
      <svg viewBox="0 0 24 24" className="h-5 w-5 inline-block mr-2" aria-hidden="true">
        <path fill="#EA4335" d="M12 10.2v3.9h5.4c-.2 1.2-.9 2.2-1.9 2.9l3 2.3c1.8-1.6 2.8-4 2.8-6.8 0-.7-.1-1.4-.2-2.1H12Z" />
        <path fill="#34A853" d="M12 21c2.7 0 4.9-.9 6.6-2.4l-3-2.3c-.8.5-1.9.9-3.6.9-2.7 0-4.9-1.8-5.7-4.2l-3.1 2.4C4.9 18.7 8.1 21 12 21Z" />
        <path fill="#4A90E2" d="M6.3 13c-.2-.5-.3-1-.3-1.6s.1-1.1.3-1.6L3.2 7.4C2.4 8.9 2 10.4 2 12s.4 3.1 1.2 4.6L6.3 13Z" />
        <path fill="#FBBC05" d="M12 6.8c1.5 0 2.8.5 3.8 1.5l2.8-2.8C16.9 3.9 14.7 3 12 3 8.1 3 4.9 5.3 3.2 8.6L6.3 11c.8-2.4 3-4.2 5.7-4.2Z" />
      </svg>
    );
  }
  if (s === "microsoft") {
    return (
      <svg viewBox="0 0 24 24" className="h-5 w-5 inline-block mr-2" aria-hidden="true">
        <path fill="#F25022" d="M3 3h8.5v8.5H3z" />
        <path fill="#7FBA00" d="M12.5 3H21v8.5h-8.5z" />
        <path fill="#00A4EF" d="M3 12.5h8.5V21H3z" />
        <path fill="#FFB900" d="M12.5 12.5H21V21h-8.5z" />
      </svg>
    );
  }
  return null;
}

export function AuthenticationTab() {
  const { t } = useI18n();
  const [providers, setProviders] = useState<OidcProviderRead[]>([]);
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState<OidcProviderRead | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState<Partial<OidcProviderCreate>>({
    slug: "",
    name: "",
    client_id: "",
    client_secret: "",
    discovery_url: "",
    scopes: "openid profile email",
    enabled: true,
    auto_provision: false,
    allowed_domains: null,
    icon_slug: null,
  });

  async function loadProviders() {
    setLoading(true);
    try {
      const data = await listOidcProviders();
      setProviders(data);
    } catch {
      /* ignore */
    } finally {
      setLoading(false);
    }
  }

  function applyPreset(slug: string) {
    const preset = PRESET_PROVIDERS[slug];
    if (preset) {
      setForm((f) => ({
        ...f,
        slug: f.slug || slug,
        name: f.name || slug.charAt(0).toUpperCase() + slug.slice(1),
        discovery_url: f.discovery_url || preset.discovery_url,
        scopes: f.scopes || preset.scopes,
        icon_slug: f.icon_slug || preset.icon_slug,
      }));
    }
  }

  async function handleCreate() {
    try {
      await createOidcProvider(form as OidcProviderCreate);
      setShowCreate(false);
      setForm({ slug: "", name: "", client_id: "", client_secret: "", discovery_url: "", scopes: "openid profile email" });
      await loadProviders();
    } catch (e: unknown) {
      alert(`Error: ${e instanceof Error ? e.message : "Failed"}`);
    }
  }

  async function handleUpdate() {
    if (!editing) return;
    try {
      await updateOidcProvider(editing.id, {
        name: form.name,
        client_id: form.client_id,
        client_secret: form.client_secret || undefined,
        discovery_url: form.discovery_url,
        scopes: form.scopes,
        enabled: form.enabled,
        auto_provision: form.auto_provision,
        allowed_domains: form.allowed_domains,
        icon_slug: form.icon_slug,
      });
      setEditing(null);
      await loadProviders();
    } catch (e: unknown) {
      alert(`Error: ${e instanceof Error ? e.message : "Failed"}`);
    }
  }

  async function handleDelete(id: string) {
    if (!confirm("Delete this OIDC provider?")) return;
    try {
      await deleteOidcProvider(id);
      await loadProviders();
    } catch {
      /* ignore */
    }
  }

  function startEdit(p: OidcProviderRead) {
    setEditing(p);
    setShowCreate(false);
    setForm({
      name: p.name,
      client_id: p.client_id,
      client_secret: "",
      discovery_url: p.discovery_url,
      scopes: p.scopes,
      enabled: p.enabled,
      auto_provision: p.auto_provision,
      allowed_domains: p.allowed_domains,
      icon_slug: p.icon_slug,
    });
  }

  // Load on first render
  if (!loading && providers.length === 0 && !showCreate && !editing) {
    loadProviders();
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-semibold">{t("settings.authentication")}</h3>
        <button
          type="button"
          className="panel-button-primary text-sm"
          onClick={() => { setShowCreate(true); setEditing(null); }}
        >
          {t("settings.addProvider")}
        </button>
      </div>

      {/* Provider list */}
      {providers.map((p) => (
        <div key={p.id} className="panel-frame space-y-2 p-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <ProviderIcon slug={p.icon_slug || p.slug} />
              <span className="font-medium">{p.name}</span>
              <span className="text-xs text-[var(--text-secondary)]">({p.slug})</span>
              {p.enabled ? (
                <span className="text-xs text-green-400">● {t("settings.enabled")}</span>
              ) : (
                <span className="text-xs text-[var(--text-secondary)]">○ {t("settings.disabled")}</span>
              )}
            </div>
            <div className="flex gap-2">
              <button type="button" className="panel-button-secondary text-xs" onClick={() => startEdit(p)}>
                {t("common.edit")}
              </button>
              <button type="button" className="panel-button-secondary text-xs text-[var(--accent)]" onClick={() => handleDelete(p.id)}>
                {t("common.delete")}
              </button>
            </div>
          </div>
          <div className="text-xs text-[var(--text-secondary)]">
            {t("settings.discoveryUrl")}: {p.discovery_url}
            {" · "}
            {t("settings.autoProvision")}: {p.auto_provision ? "✓" : "✗"}
            {p.allowed_domains && ` · ${t("settings.allowedDomains")}: ${p.allowed_domains}`}
          </div>
        </div>
      ))}

      {providers.length === 0 && !showCreate && (
        <p className="text-sm text-[var(--text-secondary)]">
          {t("settings.noProviders")}
        </p>
      )}

      {/* Create/Edit form */}
      {(showCreate || editing) && (
        <div className="panel-frame space-y-4 p-4">
          <h4 className="font-medium">
            {editing ? t("settings.editProvider") : t("settings.newProvider")}
          </h4>

          {!editing && (
            <div className="flex gap-2">
              <button type="button" className="panel-button-secondary text-xs" onClick={() => applyPreset("google")}>
                <ProviderIcon slug="google" /> Google preset
              </button>
              <button type="button" className="panel-button-secondary text-xs" onClick={() => applyPreset("microsoft")}>
                <ProviderIcon slug="microsoft" /> Microsoft 365 preset
              </button>
            </div>
          )}

          <div className="grid gap-3 md:grid-cols-2">
            {!editing && (
              <label className="panel-field">
                <span className="panel-label">Slug</span>
                <input value={form.slug || ""} onChange={(e) => setForm({ ...form, slug: e.target.value })} placeholder="google" />
              </label>
            )}
            <label className="panel-field">
              <span className="panel-label">Name</span>
              <input value={form.name || ""} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Google" />
            </label>
            <label className="panel-field">
              <span className="panel-label">Client ID</span>
              <input value={form.client_id || ""} onChange={(e) => setForm({ ...form, client_id: e.target.value })} />
            </label>
            <label className="panel-field">
              <span className="panel-label">Client Secret</span>
              <input type="password" value={form.client_secret || ""} onChange={(e) => setForm({ ...form, client_secret: e.target.value })} placeholder={editing ? "(unchanged)" : ""} />
            </label>
            <label className="panel-field md:col-span-2">
              <span className="panel-label">Discovery URL</span>
              <input value={form.discovery_url || ""} onChange={(e) => setForm({ ...form, discovery_url: e.target.value })} placeholder="https://accounts.google.com" />
            </label>
            <label className="panel-field">
              <span className="panel-label">Scopes</span>
              <input value={form.scopes || ""} onChange={(e) => setForm({ ...form, scopes: e.target.value })} />
            </label>
            <label className="panel-field">
              <span className="panel-label">Icon</span>
              <select value={form.icon_slug || ""} onChange={(e) => setForm({ ...form, icon_slug: e.target.value || null })}>
                <option value="">Default</option>
                <option value="google">Google</option>
                <option value="microsoft">Microsoft</option>
              </select>
            </label>
            <label className="panel-field md:col-span-2">
              <span className="panel-label">Allowed Domains (comma-separated, empty = all)</span>
              <input value={form.allowed_domains || ""} onChange={(e) => setForm({ ...form, allowed_domains: e.target.value || null })} placeholder="company.com, example.org" />
            </label>
          </div>

          <div className="flex items-center gap-4">
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.enabled ?? true} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
              Enabled
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.auto_provision ?? false} onChange={(e) => setForm({ ...form, auto_provision: e.target.checked })} />
              Auto-provision users
            </label>
          </div>

          <div className="flex gap-2">
            <button type="button" className="panel-button-primary text-sm" onClick={editing ? handleUpdate : handleCreate}>
              {editing ? t("common.save") : t("common.create")}
            </button>
            <button type="button" className="panel-button-secondary text-sm" onClick={() => { setEditing(null); setShowCreate(false); }}>
              {t("common.cancel")}
            </button>
          </div>
        </div>
      )}

      {/* Info note about Google/MSFT buttons */}
      <div className="text-xs text-[var(--text-secondary)] border-t border-[var(--border)] pt-4 space-y-1">
        <p>
          ℹ️ Google and Microsoft buttons are always shown on the login page for a professional enterprise appearance.
          {!providers.length && " Configure a provider above to make them functional."}
        </p>
        <p>
          Google: Create credentials at <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener" className="underline">Google Cloud Console</a>.
          Redirect URI: <code>/api/auth/oidc/callback</code>
        </p>
        <p>
          Microsoft: Register an app at <a href="https://entra.microsoft.com/#view/Microsoft_AAD_IAM/ActiveDirectoryMenuBlade/~/RegisteredApps" target="_blank" rel="noopener" className="underline">Entra ID</a>.
          Redirect URI: <code>/api/auth/oidc/callback</code>
        </p>
      </div>
    </div>
  );
}

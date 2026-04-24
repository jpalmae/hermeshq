import { FormEvent, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { resolveAssetUrl, usePublicBranding } from "../api/settings";
import { buildOidcLoginUrl, login, useAuthProviders } from "../api/auth";
import { useI18n } from "../lib/i18n";
import { useSessionStore } from "../stores/sessionStore";

function AuthProviderIcon({ provider }: { provider: string }) {
  const normalized = provider.toLowerCase();
  if (normalized === "google") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" className="h-5 w-5">
        <path fill="#EA4335" d="M12 10.2v3.9h5.4c-.2 1.2-.9 2.2-1.9 2.9l3 2.3c1.8-1.6 2.8-4 2.8-6.8 0-.7-.1-1.4-.2-2.1H12Z" />
        <path fill="#34A853" d="M12 21c2.7 0 4.9-.9 6.6-2.4l-3-2.3c-.8.5-1.9.9-3.6.9-2.7 0-4.9-1.8-5.7-4.2l-3.1 2.4C4.9 18.7 8.1 21 12 21Z" />
        <path fill="#4A90E2" d="M6.3 13c-.2-.5-.3-1-.3-1.6s.1-1.1.3-1.6L3.2 7.4C2.4 8.9 2 10.4 2 12s.4 3.1 1.2 4.6L6.3 13Z" />
        <path fill="#FBBC05" d="M12 6.8c1.5 0 2.8.5 3.8 1.5l2.8-2.8C16.9 3.9 14.7 3 12 3 8.1 3 4.9 5.3 3.2 8.6L6.3 11c.8-2.4 3-4.2 5.7-4.2Z" />
      </svg>
    );
  }
  if (normalized === "microsoft") {
    return (
      <svg viewBox="0 0 24 24" aria-hidden="true" className="h-5 w-5">
        <path fill="#F25022" d="M3 3h8.5v8.5H3z" />
        <path fill="#7FBA00" d="M12.5 3H21v8.5h-8.5z" />
        <path fill="#00A4EF" d="M3 12.5h8.5V21H3z" />
        <path fill="#FFB900" d="M12.5 12.5H21V21h-8.5z" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-5 w-5">
      <circle cx="12" cy="12" r="10" fill="currentColor" opacity="0.16" />
      <path fill="currentColor" d="M12 6.5 6.5 9v6L12 18l5.5-3V9L12 6.5Zm0 1.9 3.6 1.6L12 11.7 8.4 10 12 8.4Zm-4 2.9 3.2 1.5v3.6L8 14.9v-3.6Zm8 0v3.6l-3.2 1.5v-3.6l3.2-1.5Z" />
    </svg>
  );
}

function EnterpriseProviderList({
  providers,
  onStart,
  statusLabel,
  continueLabel,
  title,
}: {
  providers: Array<{ slug: string; name: string; enabled: boolean }>;
  onStart: (provider?: string) => void;
  statusLabel: string;
  continueLabel: (provider: string) => string;
  title: string;
}) {
  if (!providers.length) {
    return null;
  }
  return (
    <div className="space-y-3 border-t border-[var(--border)] pt-4">
      <p className="panel-label">{title}</p>
      <div className="space-y-2.5">
        {providers.map((provider) => (
          <button
            key={provider.slug}
            type="button"
            className="panel-button-secondary login-provider-button flex w-full items-center justify-between gap-3"
            onClick={() => onStart(provider.slug)}
            disabled={!provider.enabled}
            aria-disabled={!provider.enabled}
          >
            <span className="flex items-center gap-3">
              <AuthProviderIcon provider={provider.slug} />
              <span>{continueLabel(provider.name)}</span>
            </span>
            {!provider.enabled ? (
              <span className="login-provider-status">{statusLabel}</span>
            ) : null}
          </button>
        ))}
      </div>
    </div>
  );
}

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const setSession = useSessionStore((state) => state.setSession);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [showLocalLogin, setShowLocalLogin] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    return params.get("local") === "1";
  });
  const { data: branding } = usePublicBranding();
  const { data: authProviders } = useAuthProviders();
  const { t } = useI18n();
  const logoUrl = resolveAssetUrl(branding?.logo_url);
  const appName = branding?.app_name || "HermesHQ";
  const appShortName = branding?.app_short_name || appName;
  const authMode = authProviders?.auth_mode ?? "local";
  const enterpriseProviders = authProviders?.providers ?? [];
  const localLoginVisible = authMode !== "oidc" || showLocalLogin;
  const publicEnterpriseProviders = enterpriseProviders.filter(
    (provider) => provider.slug === "google" || provider.slug === "microsoft",
  );

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const token = params.get("token");
    const authError = params.get("auth_error");
    if (token) {
      setSession(token, null);
      window.history.replaceState({}, "", location.pathname);
      navigate("/", { replace: true });
      return;
    }
    if (authError) {
      setError(authError);
      window.history.replaceState({}, "", location.pathname);
    }
  }, [location.pathname, location.search, navigate, setSession]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const data = await login(username, password);
      setSession(data.access_token, null);
      navigate("/");
    } catch {
      setError(t("login.invalidCredentials"));
    } finally {
      setLoading(false);
    }
  }

  function startEnterpriseLogin(provider?: string) {
    setError(null);
    window.location.assign(buildOidcLoginUrl(provider));
  }

  return (
    <div className="login-page min-h-screen bg-[var(--black)] px-4 py-6 text-[var(--text-primary)] md:px-8 md:py-8">
      <div className="mx-auto grid max-w-[1440px] gap-8 md:grid-cols-[1.2fr_0.8fr]">
        <section className="login-hero flex min-h-[65vh] flex-col justify-between border border-[var(--border)] bg-[var(--surface)] p-8 md:p-12">
          <div>
            <p className="panel-label">{t("login.instanceBranding")}</p>
            <p className="mt-3 text-sm text-[var(--text-secondary)]">
              {t("login.globalIdentity")}
            </p>
          </div>
          <div className="space-y-6">
            <p className="panel-label">{t("login.fleetStatus")}</p>
            {logoUrl ? (
              <img src={logoUrl} alt={appName} className="h-24 w-auto max-w-[18rem] object-contain" />
            ) : (
              <h1 className="font-display text-[clamp(4rem,14vw,8rem)] leading-[0.9] text-[var(--text-display)]">
                {appShortName}
              </h1>
            )}
            <p className="max-w-[30rem] text-lg leading-relaxed text-[var(--text-primary)]">
              {t("login.heroDescription", { appName })}
            </p>
          </div>
          <div className="grid gap-6 border-t border-[var(--border)] pt-6 md:grid-cols-3">
            <div>
              <p className="panel-label">{t("login.primary")}</p>
              <p className="mt-2 text-sm text-[var(--text-secondary)]">{t("login.fleetControl")}</p>
            </div>
            <div>
              <p className="panel-label">{t("login.secondary")}</p>
              <p className="mt-2 text-sm text-[var(--text-secondary)]">{t("login.taskVisibility")}</p>
            </div>
            <div>
              <p className="panel-label">{t("login.tertiary")}</p>
              <p className="mt-2 text-sm text-[var(--text-secondary)]">{t("login.operationalTelemetry")}</p>
            </div>
          </div>
        </section>

        <section className="login-access-card panel-frame flex items-end p-8 md:p-10">
          <div className="w-full space-y-6">
            <div className="space-y-3">
              <p className="panel-label">{t("login.operatorAccess")}</p>
              <h2 className="text-3xl text-[var(--text-display)]">{t("login.authenticate", { appName })}</h2>
            </div>

            {authMode === "oidc" && !showLocalLogin ? (
              <button
                type="button"
                className="panel-button-secondary w-full"
                onClick={() => setShowLocalLogin(true)}
              >
                {t("login.useLocalAdminLogin")}
              </button>
            ) : null}

            {localLoginVisible ? (
              <form className="space-y-5 border-t border-[var(--border)] pt-6" onSubmit={onSubmit}>
                <p className="panel-label">{t("login.localOperatorAccess")}</p>
                <label className="panel-field">
                  <span className="panel-label">{t("login.username")}</span>
                  <input
                    autoComplete="username"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                  />
                </label>

                <label className="panel-field">
                  <span className="panel-label">{t("login.password")}</span>
                  <input
                    type="password"
                    autoComplete="current-password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                </label>

                <button type="submit" className="panel-button-primary w-full" disabled={loading}>
                  {loading ? t("common.loading") : t("login.enterControlSurface")}
                </button>
                <EnterpriseProviderList
                  providers={publicEnterpriseProviders}
                  onStart={startEnterpriseLogin}
                  statusLabel={t("login.notConfigured")}
                  continueLabel={(provider) => t("login.continueWithProvider", { provider })}
                  title={t("login.enterpriseAccess")}
                />
              </form>
            ) : null}

            {!localLoginVisible ? (
              <EnterpriseProviderList
                providers={publicEnterpriseProviders}
                onStart={startEnterpriseLogin}
                statusLabel={t("login.notConfigured")}
                continueLabel={(provider) => t("login.continueWithProvider", { provider })}
                title={t("login.enterpriseAccess")}
              />
            ) : null}

            {error ? <p className="panel-inline-status text-[var(--accent)]">[ERROR] {error}</p> : null}
          </div>
        </section>
      </div>
    </div>
  );
}

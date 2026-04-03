import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { resolveAssetUrl, usePublicBranding } from "../api/settings";
import { login } from "../api/auth";
import { useSessionStore } from "../stores/sessionStore";

export function LoginPage() {
  const navigate = useNavigate();
  const setSession = useSessionStore((state) => state.setSession);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const { data: branding } = usePublicBranding();
  const logoUrl = resolveAssetUrl(branding?.logo_url);
  const appName = branding?.app_name || "HermesHQ";
  const appShortName = branding?.app_short_name || appName;

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const data = await login(username, password);
      setSession(data.access_token, null);
      navigate("/");
    } catch {
      setError("Invalid operator credentials");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen bg-[var(--black)] px-4 py-6 text-[var(--text-primary)] md:px-8 md:py-8">
      <div className="mx-auto grid max-w-[1440px] gap-8 md:grid-cols-[1.2fr_0.8fr]">
        <section className="flex min-h-[65vh] flex-col justify-between border border-[var(--border)] bg-[var(--surface)] p-8 md:p-12">
          <div>
            <p className="panel-label">Instance Branding</p>
            <p className="mt-3 text-sm text-[var(--text-secondary)]">
              Global identity for this Hermes control surface.
            </p>
          </div>
          <div className="space-y-6">
            <p className="panel-label">Fleet Status</p>
            {logoUrl ? (
              <img src={logoUrl} alt={appName} className="h-24 w-auto max-w-[18rem] object-contain" />
            ) : (
              <h1 className="font-display text-[clamp(4rem,14vw,8rem)] leading-[0.9] text-[var(--text-display)]">
                {appShortName}
              </h1>
            )}
            <p className="max-w-[30rem] text-lg leading-relaxed text-[var(--text-primary)]">
              {appName} is the industrial command surface for orchestrating autonomous Hermes agents across
              long-running operational tasks.
            </p>
          </div>
          <div className="grid gap-6 border-t border-[var(--border)] pt-6 md:grid-cols-3">
            <div>
              <p className="panel-label">Primary</p>
              <p className="mt-2 text-sm text-[var(--text-secondary)]">Fleet control</p>
            </div>
            <div>
              <p className="panel-label">Secondary</p>
              <p className="mt-2 text-sm text-[var(--text-secondary)]">Task visibility</p>
            </div>
            <div>
              <p className="panel-label">Tertiary</p>
              <p className="mt-2 text-sm text-[var(--text-secondary)]">Operational telemetry</p>
            </div>
          </div>
        </section>

        <section className="panel-frame flex items-end p-8 md:p-10">
          <form className="w-full space-y-6" onSubmit={onSubmit}>
            <div className="space-y-3">
              <p className="panel-label">Operator Access</p>
              <h2 className="text-3xl text-[var(--text-display)]">Authenticate into {appName}</h2>
            </div>

            <label className="panel-field">
              <span className="panel-label">Username</span>
              <input
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              />
            </label>

            <label className="panel-field">
              <span className="panel-label">Password</span>
              <input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>

            {error ? <p className="panel-inline-status text-[var(--accent)]">[ERROR] {error}</p> : null}

            <button type="submit" className="panel-button-primary w-full" disabled={loading}>
              {loading ? "[LOADING]" : "Enter Control Surface"}
            </button>
          </form>
        </section>
      </div>
    </div>
  );
}

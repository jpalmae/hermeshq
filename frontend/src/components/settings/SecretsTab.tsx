import { FormEvent, useState } from "react";

import { useCreateSecret, useSecrets } from "../../api/secrets";
import { useProviders } from "../../api/providers";
import { useI18n } from "../../lib/i18n";
import { useSessionStore } from "../../stores/sessionStore";

export function SecretsTab() {
  const currentUser = useSessionStore((state) => state.user);
  const isAdmin = currentUser?.role === "admin";
  const { t } = useI18n();
  const { data: secrets } = useSecrets(isAdmin);
  const { data: providers } = useProviders(Boolean(currentUser));
  const createSecret = useCreateSecret();

  const [secretName, setSecretName] = useState("");
  const [secretProvider, setSecretProvider] = useState("");
  const [secretValue, setSecretValue] = useState("");

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

  return (
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
}
export default SecretsTab;

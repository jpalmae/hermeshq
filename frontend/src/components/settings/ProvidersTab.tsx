import { useEffect, useMemo, useState } from "react";

import { useProviders, useUpdateProvider } from "../../api/providers";
import { useI18n } from "../../lib/i18n";
import { useSessionStore } from "../../stores/sessionStore";

export function ProvidersTab() {
  const currentUser = useSessionStore((state) => state.user);
  const { t } = useI18n();
  const { data: providers } = useProviders(Boolean(currentUser));
  const updateProvider = useUpdateProvider();

  const [providerDrafts, setProviderDrafts] = useState<Record<string, {
    name: string;
    base_url: string;
    default_model: string;
    enabled: boolean;
  }>>({});

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

  return (
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
}

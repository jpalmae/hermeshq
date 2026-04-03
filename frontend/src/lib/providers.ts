import type { ProviderDefinition } from "../types/api";

export function findMatchingProvider(
  providers: ProviderDefinition[] | undefined,
  runtimeProvider: string | null | undefined,
  baseUrl: string | null | undefined,
) {
  if (!providers?.length || !runtimeProvider) {
    return null;
  }
  const normalizedBaseUrl = (baseUrl ?? "").trim().replace(/\/+$/, "");
  return (
    providers.find(
      (provider) =>
        provider.runtime_provider === runtimeProvider &&
        (provider.base_url ?? "").trim().replace(/\/+$/, "") === normalizedBaseUrl,
    ) ??
    providers.find((provider) => provider.runtime_provider === runtimeProvider) ??
    null
  );
}

export function applyProviderPreset(provider: ProviderDefinition, currentSecretRef = "") {
  return {
    provider: provider.runtime_provider,
    model: provider.default_model ?? "",
    base_url: provider.base_url ?? "",
    api_key_ref: provider.supports_secret_ref ? currentSecretRef : "",
  };
}

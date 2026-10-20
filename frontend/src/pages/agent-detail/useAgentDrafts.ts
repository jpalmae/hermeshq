import { useEffect, useMemo, useRef, useState } from "react";

import type {
  Agent,
  HermesVersion,
  ManagedIntegrationDefinition,
  RuntimeCapabilityOverview,
  RuntimeProfileDefinition,
  Secret,
} from "../../types/api";
import type {
  AuxiliaryDraft,
  FallbackDraft,
  IdentityForm,
  IntegrationResult,
} from "./utils";

export type AgentDraftsState = ReturnType<typeof useAgentDrafts>;

export function useAgentDrafts(
  agent: Agent | null | undefined,
  managedIntegrations: ManagedIntegrationDefinition[] | undefined,
  hermesVersions: HermesVersion[] | undefined,
  runtimeProfiles: RuntimeProfileDefinition[] | undefined,
  runtimeCapabilityOverview: RuntimeCapabilityOverview | undefined,
  secrets: Secret[] | undefined,
) {
  const [identityForm, setIdentityForm] = useState<IdentityForm>({
    friendly_name: "",
    name: "",
    slug: "",
  });
  const [systemPromptDraft, setSystemPromptDraft] = useState("");
  const [runtimeProfileDraft, setRuntimeProfileDraft] = useState("standard");
  const [hermesVersionDraft, setHermesVersionDraft] = useState("bundled");
  const [approvalModeDraft, setApprovalModeDraft] = useState("inherit");
  const [toolProgressModeDraft, setToolProgressModeDraft] = useState("inherit");
  const [thinkingModeDraft, setThinkingModeDraft] = useState("inherit");
  const [gatewayNotificationsModeDraft, setGatewayNotificationsModeDraft] = useState("inherit");
  const [useProviderDefaultDraft, setUseProviderDefaultDraft] = useState(true);
  const [customModelDraft, setCustomModelDraft] = useState("");
  const [fallbackDraft, setFallbackDraft] = useState<FallbackDraft>({ provider: null, model: null, api_key_ref: null, base_url: null });
  const [auxiliaryDraft, setAuxiliaryDraft] = useState<AuxiliaryDraft>({});
  const [integrationDrafts, setIntegrationDrafts] = useState<Record<string, Record<string, string>>>({});
  const [integrationTestResults, setIntegrationTestResults] = useState<Record<string, IntegrationResult>>({});
  const [integrationActionResults, setIntegrationActionResults] = useState<Record<string, Record<string, IntegrationResult>>>({});
  const [nameTouched, setNameTouched] = useState(false);
  const [slugTouched, setSlugTouched] = useState(false);
  const initializedAgentIdRef = useRef<string | null>(null);

  const selectedRuntimeProfile = useMemo(
    () => (runtimeProfiles ?? []).find((profile) => profile.slug === runtimeProfileDraft) ?? null,
    [runtimeProfileDraft, runtimeProfiles],
  );
  const effectiveHermesVersionEntry = useMemo(() => {
    if (!hermesVersions?.length) {
      return null;
    }
    if (agent?.hermes_version) {
      return hermesVersions.find((item) => item.version === agent.hermes_version) ?? null;
    }
    return (
      hermesVersions.find((item) => item.is_effective_default)
      ?? hermesVersions.find((item) => item.version === "bundled")
      ?? null
    );
  }, [agent?.hermes_version, hermesVersions]);
  const selectedHermesVersionEntry = useMemo(() => {
    if (!hermesVersions?.length) {
      return null;
    }
    if (hermesVersionDraft === "bundled") {
      return (
        hermesVersions.find((item) => item.is_effective_default)
        ?? hermesVersions.find((item) => item.version === "bundled")
        ?? null
      );
    }
    return hermesVersions.find((item) => item.version === hermesVersionDraft) ?? null;
  }, [hermesVersionDraft, hermesVersions]);
  const currentRuntimeCapabilityProfile = useMemo(
    () => (runtimeCapabilityOverview?.profiles ?? []).find((profile) => profile.slug === (agent?.runtime_profile || "standard")) ?? null,
    [agent?.runtime_profile, runtimeCapabilityOverview],
  );
  const enabledManagedIntegrations = useMemo(
    () => (managedIntegrations ?? []).filter((integration) => Boolean(agent?.integration_configs?.[integration.slug])),
    [agent?.integration_configs, managedIntegrations],
  );
  const secretsByProvider = useMemo(() => {
    const map = new Map<string, Secret[]>();
    for (const secret of secrets ?? []) {
      const providerKey = secret.provider || "__generic__";
      const bucket = map.get(providerKey) ?? [];
      bucket.push(secret);
      map.set(providerKey, bucket);
    }
    return map;
  }, [secrets]);

  useEffect(() => {
    if (!agent || agent.id === initializedAgentIdRef.current) {
      return;
    }
    initializedAgentIdRef.current = agent.id;
    setIdentityForm({
      friendly_name: agent.friendly_name || agent.name,
      name: agent.name,
      slug: agent.slug,
    });
    setSystemPromptDraft(agent.system_prompt ?? "");
    setRuntimeProfileDraft(agent.runtime_profile || "standard");
    setHermesVersionDraft(agent.hermes_version ?? "bundled");
    setApprovalModeDraft(agent.approval_mode ?? "inherit");
    setToolProgressModeDraft(agent.tool_progress_mode ?? "inherit");
    setThinkingModeDraft(agent.thinking_mode ?? "inherit");
    setGatewayNotificationsModeDraft(agent.gateway_notifications_mode ?? "inherit");
    setFallbackDraft({
      provider: agent.fallback_provider ?? null,
      model: agent.fallback_model ?? null,
      api_key_ref: agent.fallback_api_key_ref ?? null,
      base_url: agent.fallback_base_url ?? null,
    });
    setUseProviderDefaultDraft(agent.use_provider_default ?? true);
    setCustomModelDraft(agent.model ?? "");
    setAuxiliaryDraft(agent.auxiliary_models ?? {});
    setNameTouched(false);
    setSlugTouched(false);
  }, [agent]);

  useEffect(() => {
    if (!agent || !managedIntegrations) {
      return;
    }
    const nextIntegrationDrafts: Record<string, Record<string, string>> = {};
    for (const integration of managedIntegrations) {
      const currentConfig = (agent.integration_configs?.[integration.slug] as Record<string, unknown> | undefined) ?? {};
      nextIntegrationDrafts[integration.slug] = Object.fromEntries(
        integration.fields.map((field) => {
          const defaultValue = integration.defaults?.[field.name] ?? "";
          const currentValue = currentConfig[field.name];
          return [field.name, currentValue == null ? defaultValue : String(currentValue)];
        }),
      );
    }
    setIntegrationDrafts(nextIntegrationDrafts);
    setIntegrationTestResults({});
    setIntegrationActionResults({});
  }, [agent, managedIntegrations]);

  return {
    identityForm,
    setIdentityForm,
    systemPromptDraft,
    setSystemPromptDraft,
    runtimeProfileDraft,
    setRuntimeProfileDraft,
    hermesVersionDraft,
    setHermesVersionDraft,
    approvalModeDraft,
    setApprovalModeDraft,
    toolProgressModeDraft,
    setToolProgressModeDraft,
    thinkingModeDraft,
    setThinkingModeDraft,
    gatewayNotificationsModeDraft,
    setGatewayNotificationsModeDraft,
    useProviderDefaultDraft,
    setUseProviderDefaultDraft,
    customModelDraft,
    setCustomModelDraft,
    fallbackDraft,
    setFallbackDraft,
    auxiliaryDraft,
    setAuxiliaryDraft,
    nameTouched,
    setNameTouched,
    slugTouched,
    setSlugTouched,
    integrationDrafts,
    setIntegrationDrafts,
    integrationTestResults,
    setIntegrationTestResults,
    integrationActionResults,
    setIntegrationActionResults,
    selectedRuntimeProfile,
    effectiveHermesVersionEntry,
    selectedHermesVersionEntry,
    currentRuntimeCapabilityProfile,
    enabledManagedIntegrations,
    secretsByProvider,
  };
}

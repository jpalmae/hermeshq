import { useEffect, useMemo, useRef, useState } from "react";
import { isAxiosError } from "axios";

import {
  useMessagingChannel,
  useMessagingChannelAction,
  useMessagingChannelLogs,
  useMessagingChannelRuntime,
  useUpdateMessagingChannel,
} from "../api/messagingChannels";
import { useSecrets } from "../api/secrets";
import { useI18n } from "../lib/i18n";
import type { MessagingChannel, MessagingChannelRuntime } from "../types/api";
import {
  ChannelForm,
  defaultFormState,
  type ChannelFormState,
  type PlatformConfig,
} from "./ChannelForm";

// ---------------------------------------------------------------------------
// Utility helpers
// ---------------------------------------------------------------------------

function parseListInput(value: string) {
  return value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatList(values: string[]) {
  return values.join("\n");
}

function formatBootstrapTimestamp(value: string | null | undefined) {
  if (!value) {
    return null;
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString();
}

function getWhatsappMode(channel: MessagingChannel | undefined) {
  const metadata = channel?.metadata_json;
  if (!metadata || typeof metadata !== "object") {
    return "self-chat";
  }
  const candidate = metadata.whatsapp_mode;
  return typeof candidate === "string" && candidate.trim() ? candidate : "self-chat";
}

function buildWhatsappQrSvg(pairingQrText: string | null | undefined) {
  if (!pairingQrText) {
    return null;
  }
  const lines = pairingQrText
    .split("\n")
    .map((line) => line.replace(/\r/g, ""))
    .filter((line) => line.length > 0);
  if (!lines.length) {
    return null;
  }

  const width = Math.max(...lines.map((line) => line.length));
  const height = lines.length * 2;
  const rects: string[] = [];

  for (let y = 0; y < lines.length; y += 1) {
    const line = lines[y].padEnd(width, " ");
    for (let x = 0; x < line.length; x += 1) {
      const char = line[x];
      if (char === "█" || char === "▀") {
        rects.push(`<rect x="${x}" y="${y * 2}" width="1" height="1" fill="#111111" />`);
      }
      if (char === "█" || char === "▄") {
        rects.push(`<rect x="${x}" y="${y * 2 + 1}" width="1" height="1" fill="#111111" />`);
      }
    }
  }

  if (!rects.length) {
    return null;
  }

  const padding = 4;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width + padding * 2} ${height + padding * 2}" shape-rendering="crispEdges"><rect width="100%" height="100%" fill="#ffffff"/><g transform="translate(${padding} ${padding})">${rects.join("")}</g></svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

// ---------------------------------------------------------------------------
// Runtime summary (shared between platforms)
// ---------------------------------------------------------------------------

function RuntimeSummary({
  t,
  runtime,
}: {
  t: (key: string, vars?: Record<string, string | number>) => string;
  runtime: MessagingChannelRuntime | undefined;
}) {
  const lastBootstrapAt = formatBootstrapTimestamp(runtime?.last_bootstrap_at);
  const lastBootstrapSuccessAt = formatBootstrapTimestamp(runtime?.last_bootstrap_success_at);
  if (!runtime?.last_bootstrap_at && !runtime?.last_bootstrap_error) {
    return null;
  }
  return (
    <div className="grid gap-2 border border-[var(--border)] bg-[var(--surface-raised)] p-4 text-sm text-[var(--text-secondary)] md:grid-cols-2">
      <p>
        <span className="panel-label">{t("agent.lastBootstrapAttempt")}</span>
        <br />
        {lastBootstrapAt ?? t("agent.none")}
      </p>
      <p>
        <span className="panel-label">{t("agent.lastBootstrapSuccess")}</span>
        <br />
        {lastBootstrapSuccessAt ?? t("agent.none")}
      </p>
      <p>
        <span className="panel-label">{t("agent.bootstrapAttempts")}</span>
        <br />
        {runtime?.last_bootstrap_attempts ?? 0}
      </p>
      <p>
        <span className="panel-label">{t("agent.bootstrapDuration")}</span>
        <br />
        {runtime?.last_bootstrap_duration_ms != null ? `${runtime.last_bootstrap_duration_ms} ms` : t("agent.none")}
      </p>
      {runtime?.last_bootstrap_error ? (
        <p className="md:col-span-2">
          <span className="panel-label">{t("agent.lastBootstrapError")}</span>
          <br />
          {runtime.last_bootstrap_error}
        </p>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Platform configuration descriptors
// ---------------------------------------------------------------------------

type PlatformSlug = "telegram" | "whatsapp" | "microsoft_teams" | "google_chat";

const COPY_KEYS: Record<PlatformSlug, string> = {
  telegram: "agent.telegramCopy",
  whatsapp: "agent.whatsappCopy",
  microsoft_teams: "agent.teamsCopy",
  google_chat: "agent.googleChatCopy",
};

const PLATFORM_CONFIGS: Record<PlatformSlug, PlatformConfig> = {
  telegram: {
    platform: "telegram",
    label: "Telegram",
    copy: "",
    showSecretRef: true,
    showWhatsappMode: false,
    showQrSection: false,
    showAllowedUsers: true,
    showHomeChat: true,
    showBehavior: true,
    homeChatIdPlaceholder: "-1001234567890",
    enableLabelKey: "agent.enableTelegram",
    saveLabelKey: "agent.saveTelegram",
    stoppedLabelKey: "agent.gatewayStopped",
  },
  whatsapp: {
    platform: "whatsapp",
    label: "WhatsApp",
    copy: "",
    showSecretRef: false,
    showWhatsappMode: true,
    showQrSection: true,
    showAllowedUsers: true,
    showHomeChat: true,
    showBehavior: true,
    homeChatIdPlaceholder: "56912345678@s.whatsapp.net",
    enableLabelKey: "agent.enableWhatsapp",
    saveLabelKey: "agent.saveWhatsapp",
    stoppedLabelKey: "agent.gatewayStopped",
  },
  microsoft_teams: {
    platform: "microsoft_teams",
    label: "Microsoft Teams",
    copy: "",
    showSecretRef: true,
    showWhatsappMode: false,
    showQrSection: false,
    showAllowedUsers: false,
    showHomeChat: false,
    showBehavior: false,
    homeChatIdPlaceholder: "",
    enableLabelKey: "agent.enableTeams",
    saveLabelKey: "agent.saveTeams",
    stoppedLabelKey: "agent.gatewayStopped",
  },
  google_chat: {
    platform: "google_chat",
    label: "Google Chat",
    copy: "",
    showSecretRef: true,
    showWhatsappMode: false,
    showQrSection: false,
    showAllowedUsers: false,
    showHomeChat: false,
    showBehavior: false,
    homeChatIdPlaceholder: "",
    enableLabelKey: "agent.enableGoogleChat",
    saveLabelKey: "agent.saveGoogleChat",
    stoppedLabelKey: "agent.gatewayStopped",
  },
};

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

const ALL_PLATFORMS: PlatformSlug[] = ["telegram", "whatsapp", "microsoft_teams", "google_chat"];

const PLATFORM_LABELS: Record<PlatformSlug, string> = {
  telegram: "Telegram",
  whatsapp: "WhatsApp",
  microsoft_teams: "MS Teams",
  google_chat: "Google Chat",
};

export function AgentMessagingPanel({ agentId, isAdmin }: { agentId: string; isAdmin: boolean }) {
  const { t } = useI18n();
  // Hooks for all 4 platforms
  const { data: telegram } = useMessagingChannel(agentId, "telegram");
  const { data: telegramRuntime } = useMessagingChannelRuntime(agentId, "telegram");
  const { data: telegramLogs } = useMessagingChannelLogs(agentId, "telegram");
  const { data: whatsapp } = useMessagingChannel(agentId, "whatsapp");
  const { data: whatsappRuntime } = useMessagingChannelRuntime(agentId, "whatsapp");
  const { data: whatsappLogs } = useMessagingChannelLogs(agentId, "whatsapp");
  const { data: teams } = useMessagingChannel(agentId, "microsoft_teams");
  const { data: teamsRuntime } = useMessagingChannelRuntime(agentId, "microsoft_teams");
  const { data: teamsLogs } = useMessagingChannelLogs(agentId, "microsoft_teams");
  const { data: gchat } = useMessagingChannel(agentId, "google_chat");
  const { data: gchatRuntime } = useMessagingChannelRuntime(agentId, "google_chat");
  const { data: gchatLogs } = useMessagingChannelLogs(agentId, "google_chat");

  const { data: secrets } = useSecrets(isAdmin);
  const updateChannel = useUpdateMessagingChannel();
  const startChannel = useMessagingChannelAction("start");
  const stopChannel = useMessagingChannelAction("stop");
  const [submitErrors, setSubmitErrors] = useState<Record<string, string | null>>({});

  // Per-platform dirty refs and forms
  const dirtyRefs = useRef<Record<PlatformSlug, boolean>>({ telegram: false, whatsapp: false, microsoft_teams: false, google_chat: false });
  const [forms, setForms] = useState<Record<PlatformSlug, ChannelFormState>>({
    telegram: defaultFormState,
    whatsapp: defaultFormState,
    microsoft_teams: defaultFormState,
    google_chat: defaultFormState,
  });
  const [selectedPlatform, setSelectedPlatform] = useState<PlatformSlug>("telegram");
  const whatsappQrSvg = useMemo(
    () => buildWhatsappQrSvg(whatsappRuntime?.pairing_qr_text),
    [whatsappRuntime?.pairing_qr_text],
  );

  const isActive = selectedPlatform === "telegram";

  // Channel data map for generic access
  const channelData: Record<PlatformSlug, MessagingChannel | undefined> = {
    telegram, whatsapp, microsoft_teams: teams, google_chat: gchat,
  };
  const runtimeData: Record<PlatformSlug, MessagingChannelRuntime | undefined> = {
    telegram: telegramRuntime, whatsapp: whatsappRuntime, microsoft_teams: teamsRuntime, google_chat: gchatRuntime,
  };
  const logsData: Record<PlatformSlug, string | undefined> = {
    telegram: telegramLogs, whatsapp: whatsappLogs, microsoft_teams: teamsLogs, google_chat: gchatLogs,
  };

  // Generic sync effect for all platforms
  useEffect(() => {
    for (const p of ALL_PLATFORMS) {
      const ch = channelData[p];
      if (!ch || dirtyRefs.current[p]) continue;
      const newForm: ChannelFormState = {
        enabled: ch.enabled,
        secret_ref: ch.secret_ref ?? "",
        allowed_user_ids: formatList(ch.allowed_user_ids),
        home_chat_id: ch.home_chat_id ?? "",
        home_chat_name: ch.home_chat_name ?? "",
        require_mention: ch.require_mention,
        free_response_chat_ids: formatList(ch.free_response_chat_ids),
        unauthorized_dm_behavior: ch.unauthorized_dm_behavior ?? "pair",
        whatsapp_mode: p === "whatsapp" ? getWhatsappMode(ch) : "self-chat",
      };
      setForms((prev) => (prev[p] === newForm ? prev : { ...prev, [p]: newForm }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [telegram, whatsapp, teams, gchat]);

  // Secret options
  const secretOptions = useMemo(
    () =>
      (secrets ?? [])
        .filter((item) => {
          const provider = String(item.provider ?? "").trim().toLowerCase();
          return !provider || provider === "telegram" || provider === "microsoft_teams" || provider === "google_chat";
        })
        .map((item) => String(item.name ?? ""))
        .filter(Boolean)
        .sort((left, right) => left.localeCompare(right)),
    [secrets],
  );

  // Error handling
  function describeError(error: unknown, fallback: string) {
    if (isAxiosError<{ detail?: string }>(error)) {
      const detail = error.response?.data?.detail;
      if (typeof detail === "string" && detail.trim()) {
        return detail;
      }
    }
    if (error instanceof Error && error.message.trim()) {
      return error.message;
    }
    return fallback;
  }

  function clearError(platform: string) {
    setSubmitErrors((current) => ({ ...current, [platform]: null }));
  }

  // Save handlers
  // Save handler (generic for all platforms)
  async function savePlatform(platform: PlatformSlug) {
    clearError(platform);
    const form = forms[platform];
    try {
      const payload: Record<string, unknown> = {
        enabled: form.enabled,
        secret_ref: form.secret_ref || null,
        allowed_user_ids: parseListInput(form.allowed_user_ids),
        home_chat_id: form.home_chat_id || null,
        home_chat_name: form.home_chat_name || null,
        require_mention: form.require_mention,
        free_response_chat_ids: parseListInput(form.free_response_chat_ids),
        unauthorized_dm_behavior: form.unauthorized_dm_behavior,
      };
      if (platform === "whatsapp") {
        payload.metadata_json = { whatsapp_mode: form.whatsapp_mode };
      }
      await updateChannel.mutateAsync({ agentId, platform, payload });
      dirtyRefs.current[platform] = false;
    } catch (error) {
      setSubmitErrors((current) => ({
        ...current,
        [platform]: describeError(error, t("agent.gatewayConfigSaveFailed")),
      }));
    }
  }

  async function startPlatform(platform: PlatformSlug) {
    clearError(platform);
    try {
      await startChannel.mutateAsync({ agentId, platform });
    } catch (error) {
      setSubmitErrors((current) => ({
        ...current,
        [platform]: describeError(error, t("agent.gatewayStartFailed")),
      }));
    }
  }

  async function stopPlatform(platform: PlatformSlug) {
    clearError(platform);
    try {
      await stopChannel.mutateAsync({ agentId, platform });
    } catch (error) {
      setSubmitErrors((current) => ({
        ...current,
        [platform]: describeError(error, t("agent.gatewayStopFailed")),
      }));
    }
  }

  // Build platform tab data
  const platforms = ALL_PLATFORMS.map((p) => ({
    platform: p,
    label: PLATFORM_LABELS[p],
    status: runtimeData[p]?.status ?? channelData[p]?.status ?? "stopped",
  }));

  // Active platform data (generic)
  const activeConfig = { ...PLATFORM_CONFIGS[selectedPlatform] };
  activeConfig.copy = t(COPY_KEYS[selectedPlatform]);
  const activeForm = forms[selectedPlatform];
  const activeSetForm = (update: React.SetStateAction<ChannelFormState>) => {
    dirtyRefs.current[selectedPlatform] = true;
    setForms((prev) => ({ ...prev, [selectedPlatform]: typeof update === "function" ? update(prev[selectedPlatform]) : update }));
  };
  const activeRuntime = runtimeData[selectedPlatform];
  const activeStatus = runtimeData[selectedPlatform]?.status ?? channelData[selectedPlatform]?.status ?? "stopped";
  const activeLogs = logsData[selectedPlatform];

  return (
    <div className="mt-6 border-t border-[var(--border)] pt-6">
      <div className="border-b border-[var(--border)] pb-4">
        <p className="panel-label">{t("agent.messagingChannels")}</p>
      </div>

      <div className="mt-5">
        {/* Platform selector tabs */}
        <div className="flex flex-wrap gap-2 rounded-[1.25rem] border border-[var(--border)] bg-[color-mix(in_srgb,var(--surface)_92%,transparent)] p-2">
          {platforms.map((item) => {
            const active = selectedPlatform === item.platform;
            return (
              <button
                key={item.platform}
                type="button"
                className={`rounded-full px-4 py-3 text-left transition ${
                  active
                    ? "border border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_16%,transparent)] text-[var(--text-display)] shadow-[0_0_0_1px_color-mix(in_srgb,var(--accent)_28%,transparent)]"
                    : "border border-transparent bg-[var(--surface)] text-[var(--text-secondary)] hover:border-[var(--border-visible)] hover:text-[var(--text-display)]"
                }`}
                onClick={() => setSelectedPlatform(item.platform)}
              >
                <span className="block text-sm font-medium">{item.label}</span>
                <span className="mt-1 block font-mono text-[11px] uppercase tracking-[0.12em] opacity-80">
                  {item.status}
                </span>
              </button>
            );
          })}
        </div>

        <div className="mt-5">
          <ChannelForm
            config={activeConfig}
            form={activeForm}
            setForm={activeSetForm}
            runtime={activeRuntime}
            runtimeStatus={activeStatus}
            lastError={submitErrors[selectedPlatform]}
            secretOptions={secretOptions}
            isAdmin={isAdmin}
            isUpdatePending={updateChannel.isPending}
            isStartPending={startChannel.isPending}
            isStopPending={stopChannel.isPending}
            qrSvg={whatsappQrSvg}
            pairingStatus={whatsappRuntime?.pairing_status ?? null}
            sessionPath={whatsappRuntime?.session_path ?? null}
            bridgeLogPath={whatsappRuntime?.bridge_log_path ?? null}
            pairingQrText={whatsappRuntime?.pairing_qr_text ?? null}
            onSave={() => void savePlatform(selectedPlatform)}
            onStart={() => void startPlatform(selectedPlatform)}
            onStop={() => void stopPlatform(selectedPlatform)}
          />

          {/* Gateway logs */}
          <div className="mt-5 border-t border-[var(--border)] pt-4">
            <p className="panel-label">{t("agent.gatewayLogTail")}</p>
            <pre className="mt-3 max-h-60 overflow-auto whitespace-pre-wrap border border-[var(--border)] bg-[var(--surface-raised)] p-4 text-xs leading-6 text-[var(--text-secondary)]">
              {activeLogs?.trim()
                ? activeLogs
                : t("agent.noGatewayOutput")}
            </pre>
          </div>

          {/* Runtime summary */}
          <div className="mt-4">
            <RuntimeSummary t={t} runtime={activeRuntime} />
          </div>
        </div>
      </div>
    </div>
  );
}

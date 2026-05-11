import { useEffect, useMemo, useState } from "react";
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

const PLATFORM_CONFIGS: Record<"telegram" | "whatsapp", PlatformConfig> = {
  telegram: {
    platform: "telegram",
    label: "Telegram",
    copy: "", // set dynamically via t()
    showSecretRef: true,
    showWhatsappMode: false,
    showQrSection: false,
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
    homeChatIdPlaceholder: "56912345678@s.whatsapp.net",
    enableLabelKey: "agent.enableWhatsapp",
    saveLabelKey: "agent.saveWhatsapp",
    stoppedLabelKey: "agent.gatewayStopped",
  },
};

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function AgentMessagingPanel({ agentId, isAdmin }: { agentId: string; isAdmin: boolean }) {
  const { t } = useI18n();
  const { data: telegram } = useMessagingChannel(agentId, "telegram");
  const { data: telegramRuntime } = useMessagingChannelRuntime(agentId, "telegram");
  const { data: telegramLogs } = useMessagingChannelLogs(agentId, "telegram");
  const { data: whatsapp } = useMessagingChannel(agentId, "whatsapp");
  const { data: whatsappRuntime } = useMessagingChannelRuntime(agentId, "whatsapp");
  const { data: whatsappLogs } = useMessagingChannelLogs(agentId, "whatsapp");
  const { data: secrets } = useSecrets(isAdmin);
  const updateChannel = useUpdateMessagingChannel();
  const startChannel = useMessagingChannelAction("start");
  const stopChannel = useMessagingChannelAction("stop");
  const [submitErrors, setSubmitErrors] = useState<Record<string, string | null>>({});
  const [telegramForm, setTelegramForm] = useState<ChannelFormState>(defaultFormState);
  const [whatsappForm, setWhatsappForm] = useState<ChannelFormState>(defaultFormState);
  const [selectedPlatform, setSelectedPlatform] = useState<"telegram" | "whatsapp">("telegram");
  const whatsappQrSvg = useMemo(
    () => buildWhatsappQrSvg(whatsappRuntime?.pairing_qr_text),
    [whatsappRuntime?.pairing_qr_text],
  );

  // Sync form state from server data
  useEffect(() => {
    if (!telegram) {
      return;
    }
    setTelegramForm({
      enabled: telegram.enabled,
      secret_ref: telegram.secret_ref ?? "",
      allowed_user_ids: formatList(telegram.allowed_user_ids),
      home_chat_id: telegram.home_chat_id ?? "",
      home_chat_name: telegram.home_chat_name ?? "",
      require_mention: telegram.require_mention,
      free_response_chat_ids: formatList(telegram.free_response_chat_ids),
      unauthorized_dm_behavior: telegram.unauthorized_dm_behavior ?? "pair",
      whatsapp_mode: "self-chat",
    });
  }, [telegram]);

  useEffect(() => {
    if (!whatsapp) {
      return;
    }
    setWhatsappForm({
      enabled: whatsapp.enabled,
      secret_ref: whatsapp.secret_ref ?? "",
      allowed_user_ids: formatList(whatsapp.allowed_user_ids),
      home_chat_id: whatsapp.home_chat_id ?? "",
      home_chat_name: whatsapp.home_chat_name ?? "",
      require_mention: whatsapp.require_mention,
      free_response_chat_ids: formatList(whatsapp.free_response_chat_ids),
      unauthorized_dm_behavior: whatsapp.unauthorized_dm_behavior ?? "pair",
      whatsapp_mode: getWhatsappMode(whatsapp),
    });
  }, [whatsapp]);

  // Secret options
  const secretOptions = useMemo(
    () =>
      (secrets ?? [])
        .filter((item) => {
          const provider = String(item.provider ?? "").trim().toLowerCase();
          return !provider || provider === "telegram";
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
  async function saveTelegram() {
    clearError("telegram");
    try {
      await updateChannel.mutateAsync({
        agentId,
        platform: "telegram",
        payload: {
          enabled: telegramForm.enabled,
          secret_ref: telegramForm.secret_ref || null,
          allowed_user_ids: parseListInput(telegramForm.allowed_user_ids),
          home_chat_id: telegramForm.home_chat_id || null,
          home_chat_name: telegramForm.home_chat_name || null,
          require_mention: telegramForm.require_mention,
          free_response_chat_ids: parseListInput(telegramForm.free_response_chat_ids),
          unauthorized_dm_behavior: telegramForm.unauthorized_dm_behavior,
        },
      });
    } catch (error) {
      setSubmitErrors((current) => ({
        ...current,
        telegram: describeError(error, t("agent.gatewayConfigSaveFailed")),
      }));
    }
  }

  async function saveWhatsapp() {
    clearError("whatsapp");
    try {
      await updateChannel.mutateAsync({
        agentId,
        platform: "whatsapp",
        payload: {
          enabled: whatsappForm.enabled,
          allowed_user_ids: parseListInput(whatsappForm.allowed_user_ids),
          home_chat_id: whatsappForm.home_chat_id || null,
          home_chat_name: whatsappForm.home_chat_name || null,
          require_mention: whatsappForm.require_mention,
          free_response_chat_ids: parseListInput(whatsappForm.free_response_chat_ids),
          metadata_json: {
            whatsapp_mode: whatsappForm.whatsapp_mode,
          },
        },
      });
    } catch (error) {
      setSubmitErrors((current) => ({
        ...current,
        whatsapp: describeError(error, t("agent.whatsappConfigSaveFailed")),
      }));
    }
  }

  async function startPlatform(platform: "telegram" | "whatsapp") {
    clearError(platform);
    try {
      await startChannel.mutateAsync({ agentId, platform });
    } catch (error) {
      setSubmitErrors((current) => ({
        ...current,
        [platform]: describeError(
          error,
          platform === "telegram" ? t("agent.gatewayStartFailed") : t("agent.whatsappStartFailed"),
        ),
      }));
    }
  }

  async function stopPlatform(platform: "telegram" | "whatsapp") {
    clearError(platform);
    try {
      await stopChannel.mutateAsync({ agentId, platform });
    } catch (error) {
      setSubmitErrors((current) => ({
        ...current,
        [platform]: describeError(
          error,
          platform === "telegram" ? t("agent.gatewayStopFailed") : t("agent.whatsappStopFailed"),
        ),
      }));
    }
  }

  const telegramStatus = telegramRuntime?.status ?? telegram?.status ?? "stopped";
  const whatsappStatus = whatsappRuntime?.status ?? whatsapp?.status ?? "stopped";

  // Platform tab data
  const platforms = [
    { platform: "telegram" as const, label: "Telegram", status: telegramStatus },
    { platform: "whatsapp" as const, label: "WhatsApp", status: whatsappStatus },
  ];

  // Active platform data
  const isActive = selectedPlatform === "telegram";
  const activeConfig = { ...PLATFORM_CONFIGS[selectedPlatform] };
  activeConfig.copy = isActive ? t("agent.telegramCopy") : t("agent.whatsappCopy");
  const activeForm = isActive ? telegramForm : whatsappForm;
  const activeSetForm = isActive ? setTelegramForm : setWhatsappForm;
  const activeRuntime = isActive ? telegramRuntime : whatsappRuntime;
  const activeStatus = isActive ? telegramStatus : whatsappStatus;
  const activeLogs = isActive ? telegramLogs : whatsappLogs;

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
            onSave={() => void (isActive ? saveTelegram() : saveWhatsapp())}
            onStart={() => void startPlatform(selectedPlatform)}
            onStop={() => void stopPlatform(selectedPlatform)}
          />

          {/* Gateway logs */}
          <div className="mt-5 border-t border-[var(--border)] pt-4">
            <p className="panel-label">{t("agent.gatewayLogTail")}</p>
            <pre className="mt-3 max-h-60 overflow-auto whitespace-pre-wrap border border-[var(--border)] bg-[var(--surface-raised)] p-4 text-xs leading-6 text-[var(--text-secondary)]">
              {activeLogs?.trim()
                ? activeLogs
                : isActive
                  ? t("agent.noGatewayOutput")
                  : t("agent.noWhatsappGatewayOutput")}
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

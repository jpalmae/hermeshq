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

type ChannelFormState = {
  enabled: boolean;
  secret_ref: string;
  allowed_user_ids: string;
  home_chat_id: string;
  home_chat_name: string;
  require_mention: boolean;
  free_response_chat_ids: string;
  unauthorized_dm_behavior: string;
  whatsapp_mode: string;
};

const defaultFormState: ChannelFormState = {
  enabled: false,
  secret_ref: "",
  allowed_user_ids: "",
  home_chat_id: "",
  home_chat_name: "",
  require_mention: false,
  free_response_chat_ids: "",
  unauthorized_dm_behavior: "pair",
  whatsapp_mode: "self-chat",
};

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
  const whatsappQrSvg = useMemo(
    () => buildWhatsappQrSvg(whatsappRuntime?.pairing_qr_text),
    [whatsappRuntime?.pairing_qr_text],
  );

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
      secret_ref: "",
      allowed_user_ids: formatList(whatsapp.allowed_user_ids),
      home_chat_id: whatsapp.home_chat_id ?? "",
      home_chat_name: whatsapp.home_chat_name ?? "",
      require_mention: whatsapp.require_mention,
      free_response_chat_ids: formatList(whatsapp.free_response_chat_ids),
      unauthorized_dm_behavior: whatsapp.unauthorized_dm_behavior ?? "pair",
      whatsapp_mode: getWhatsappMode(whatsapp),
    });
  }, [whatsapp]);

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

  const telegramSecretOptions = useMemo(() => {
    if (telegramForm.secret_ref && !secretOptions.includes(telegramForm.secret_ref)) {
      return [telegramForm.secret_ref, ...secretOptions];
    }
    return secretOptions;
  }, [telegramForm.secret_ref, secretOptions]);

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

  return (
    <div className="mt-6 border-t border-[var(--border)] pt-6">
      <div className="border-b border-[var(--border)] pb-4">
        <p className="panel-label">{t("agent.messagingChannels")}</p>
      </div>

      <div className="mt-5 grid gap-6 xl:grid-cols-2">
        <section className="panel-frame border border-[var(--border)] p-5">
          <div className="flex items-center justify-between gap-4 border-b border-[var(--border)] pb-4">
            <div>
              <p className="text-lg text-[var(--text-display)]">Telegram</p>
              <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">{t("agent.telegramCopy")}</p>
            </div>
            <div className="text-right">
              <p className="panel-label">{t("agent.runtime")}</p>
              <p className="mt-2 text-sm uppercase tracking-[0.1em] text-[var(--text-display)]">{telegramStatus}</p>
              {telegramRuntime?.last_bootstrap_status ? (
                <p className="mt-2 text-xs text-[var(--text-secondary)]">
                  {t("agent.bootstrapStatus")}: {telegramRuntime.last_bootstrap_status}
                </p>
              ) : null}
            </div>
          </div>

          {isAdmin ? (
            <div className="mt-5 space-y-4">
              <label className="panel-field">
                <span className="panel-label">{t("agent.botTokenSecretRef")}</span>
                <select
                  value={telegramForm.secret_ref}
                  onChange={(event) => setTelegramForm((current) => ({ ...current, secret_ref: event.target.value }))}
                >
                  <option value="">{t("agent.selectTelegramSecret")}</option>
                  {telegramSecretOptions.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
                <p className="panel-inline-status">{t("agent.telegramSecretHint")}</p>
              </label>

              <label className="panel-field">
                <span className="panel-label">{t("agent.allowedTelegramUsers")}</span>
                <textarea
                  rows={3}
                  value={telegramForm.allowed_user_ids}
                  onChange={(event) =>
                    setTelegramForm((current) => ({ ...current, allowed_user_ids: event.target.value }))
                  }
                  placeholder={"123456789\n987654321"}
                />
              </label>

              <div className="grid gap-4 md:grid-cols-2">
                <label className="panel-field">
                  <span className="panel-label">{t("agent.homeChatId")}</span>
                  <input
                    value={telegramForm.home_chat_id}
                    onChange={(event) =>
                      setTelegramForm((current) => ({ ...current, home_chat_id: event.target.value }))
                    }
                    placeholder="-1001234567890"
                  />
                </label>
                <label className="panel-field">
                  <span className="panel-label">{t("agent.homeChatName")}</span>
                  <input
                    value={telegramForm.home_chat_name}
                    onChange={(event) =>
                      setTelegramForm((current) => ({ ...current, home_chat_name: event.target.value }))
                    }
                    placeholder="Newsroom"
                  />
                </label>
              </div>

              <label className="panel-field">
                <span className="panel-label">{t("agent.freeResponseChatIds")}</span>
                <textarea
                  rows={2}
                  value={telegramForm.free_response_chat_ids}
                  onChange={(event) =>
                    setTelegramForm((current) => ({ ...current, free_response_chat_ids: event.target.value }))
                  }
                  placeholder="-1001234567890"
                />
              </label>

              <div className="grid gap-4 md:grid-cols-2">
                <label className="panel-field">
                  <span className="panel-label">{t("agent.unauthorizedDmBehavior")}</span>
                  <select
                    value={telegramForm.unauthorized_dm_behavior}
                    onChange={(event) =>
                      setTelegramForm((current) => ({ ...current, unauthorized_dm_behavior: event.target.value }))
                    }
                  >
                    <option value="pair">pair</option>
                    <option value="ignore">ignore</option>
                  </select>
                </label>
                <label className="panel-field justify-end">
                  <span className="panel-label">{t("agent.mentionGating")}</span>
                  <label className="mt-3 flex items-center gap-3 text-sm text-[var(--text-primary)]">
                    <input
                      checked={telegramForm.require_mention}
                      onChange={(event) =>
                        setTelegramForm((current) => ({ ...current, require_mention: event.target.checked }))
                      }
                      type="checkbox"
                    />
                    {t("agent.requireMention")}
                  </label>
                </label>
              </div>

              <label className="mt-2 flex items-center gap-3 text-sm text-[var(--text-primary)]">
                <input
                  checked={telegramForm.enabled}
                  onChange={(event) => setTelegramForm((current) => ({ ...current, enabled: event.target.checked }))}
                  type="checkbox"
                />
                {t("agent.enableTelegram")}
              </label>

              <div className="flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  className="panel-button-secondary"
                  onClick={() => void saveTelegram()}
                  disabled={updateChannel.isPending}
                >
                  {updateChannel.isPending ? t("common.loading") : t("agent.saveTelegram")}
                </button>
                <button
                  type="button"
                  className="panel-button-secondary"
                  onClick={() => void startPlatform("telegram")}
                  disabled={startChannel.isPending}
                >
                  {t("agent.startGateway")}
                </button>
                <button
                  type="button"
                  className="panel-button-secondary"
                  onClick={() => void stopPlatform("telegram")}
                  disabled={stopChannel.isPending}
                >
                  {t("agent.stopGateway")}
                </button>
                <p className="panel-inline-status">
                  {telegramRuntime?.status === "running"
                    ? `[LIVE] telegram gateway pid ${telegramRuntime.pid ?? "?"}`
                    : submitErrors.telegram || telegram?.last_error || t("agent.gatewayStopped")}
                </p>
              </div>
              <RuntimeSummary t={t} runtime={telegramRuntime} />
            </div>
          ) : (
            <div className="mt-5 space-y-3 text-sm leading-6 text-[var(--text-secondary)]">
              <p>
                {t("agent.enabled")}: {telegram?.enabled ? t("agent.yes") : t("agent.no")}
              </p>
              <p>
                {t("agent.allowedUsers")}:{" "}
                {telegram?.allowed_user_ids?.length ? telegram.allowed_user_ids.join(", ") : t("agent.none")}
              </p>
              <p>
                {t("agent.homeChat")}: {telegram?.home_chat_id ?? t("agent.none")}
              </p>
            </div>
          )}

          <div className="mt-5 border-t border-[var(--border)] pt-4">
            <p className="panel-label">{t("agent.gatewayLogTail")}</p>
            <pre className="mt-3 max-h-60 overflow-auto whitespace-pre-wrap border border-[var(--border)] bg-[var(--surface-raised)] p-4 text-xs leading-6 text-[var(--text-secondary)]">
              {telegramLogs?.trim() ? telegramLogs : t("agent.noGatewayOutput")}
            </pre>
          </div>
        </section>

        <section className="panel-frame border border-[var(--border)] p-5">
          <div className="flex items-center justify-between gap-4 border-b border-[var(--border)] pb-4">
            <div>
              <p className="text-lg text-[var(--text-display)]">WhatsApp</p>
              <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">{t("agent.whatsappCopy")}</p>
            </div>
            <div className="text-right">
              <p className="panel-label">{t("agent.runtime")}</p>
              <p className="mt-2 text-sm uppercase tracking-[0.1em] text-[var(--text-display)]">{whatsappStatus}</p>
              {whatsappRuntime?.pairing_status ? (
                <p className="mt-2 text-xs text-[var(--text-secondary)]">
                  {t("agent.whatsappPairingStatus")}: {whatsappRuntime.pairing_status}
                </p>
              ) : null}
            </div>
          </div>

          {isAdmin ? (
            <div className="mt-5 space-y-4">
              <label className="panel-field">
                <span className="panel-label">{t("agent.whatsappMode")}</span>
                <select
                  value={whatsappForm.whatsapp_mode}
                  onChange={(event) =>
                    setWhatsappForm((current) => ({ ...current, whatsapp_mode: event.target.value }))
                  }
                >
                  <option value="self-chat">{t("agent.whatsappModeSelfChat")}</option>
                  <option value="bot">{t("agent.whatsappModeBot")}</option>
                </select>
              </label>

              <label className="panel-field">
                <span className="panel-label">{t("agent.allowedWhatsappUsers")}</span>
                <textarea
                  rows={3}
                  value={whatsappForm.allowed_user_ids}
                  onChange={(event) =>
                    setWhatsappForm((current) => ({ ...current, allowed_user_ids: event.target.value }))
                  }
                  placeholder={"56912345678\n*"}
                />
              </label>

              <div className="grid gap-4 md:grid-cols-2">
                <label className="panel-field">
                  <span className="panel-label">{t("agent.homeChatId")}</span>
                  <input
                    value={whatsappForm.home_chat_id}
                    onChange={(event) =>
                      setWhatsappForm((current) => ({ ...current, home_chat_id: event.target.value }))
                    }
                    placeholder="56912345678@s.whatsapp.net"
                  />
                </label>
                <label className="panel-field">
                  <span className="panel-label">{t("agent.homeChatName")}</span>
                  <input
                    value={whatsappForm.home_chat_name}
                    onChange={(event) =>
                      setWhatsappForm((current) => ({ ...current, home_chat_name: event.target.value }))
                    }
                    placeholder="Primary number"
                  />
                </label>
              </div>

              <label className="panel-field">
                <span className="panel-label">{t("agent.freeResponseChatIds")}</span>
                <textarea
                  rows={2}
                  value={whatsappForm.free_response_chat_ids}
                  onChange={(event) =>
                    setWhatsappForm((current) => ({ ...current, free_response_chat_ids: event.target.value }))
                  }
                  placeholder="120363000000000000@g.us"
                />
              </label>

              <label className="panel-field justify-end">
                <span className="panel-label">{t("agent.mentionGating")}</span>
                <label className="mt-3 flex items-center gap-3 text-sm text-[var(--text-primary)]">
                  <input
                    checked={whatsappForm.require_mention}
                    onChange={(event) =>
                      setWhatsappForm((current) => ({ ...current, require_mention: event.target.checked }))
                    }
                    type="checkbox"
                  />
                  {t("agent.requireMention")}
                </label>
              </label>

              <label className="mt-2 flex items-center gap-3 text-sm text-[var(--text-primary)]">
                <input
                  checked={whatsappForm.enabled}
                  onChange={(event) => setWhatsappForm((current) => ({ ...current, enabled: event.target.checked }))}
                  type="checkbox"
                />
                {t("agent.enableWhatsapp")}
              </label>

              <div className="flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  className="panel-button-secondary"
                  onClick={() => void saveWhatsapp()}
                  disabled={updateChannel.isPending}
                >
                  {updateChannel.isPending ? t("common.loading") : t("agent.saveWhatsapp")}
                </button>
                <button
                  type="button"
                  className="panel-button-secondary"
                  onClick={() => void startPlatform("whatsapp")}
                  disabled={startChannel.isPending}
                >
                  {t("agent.startGateway")}
                </button>
                <button
                  type="button"
                  className="panel-button-secondary"
                  onClick={() => void stopPlatform("whatsapp")}
                  disabled={stopChannel.isPending}
                >
                  {t("agent.stopGateway")}
                </button>
                <p className="panel-inline-status">
                  {whatsappRuntime?.status === "running"
                    ? `[LIVE] whatsapp gateway pid ${whatsappRuntime.pid ?? "?"}`
                    : submitErrors.whatsapp || whatsapp?.last_error || t("agent.gatewayStopped")}
                </p>
              </div>

              <RuntimeSummary t={t} runtime={whatsappRuntime} />

              <div className="grid gap-2 border border-[var(--border)] bg-[var(--surface-raised)] p-4 text-sm text-[var(--text-secondary)]">
                <p>
                  <span className="panel-label">{t("agent.whatsappPairingStatus")}</span>
                  <br />
                  {whatsappRuntime?.pairing_status ?? t("agent.none")}
                </p>
                {whatsappRuntime?.pairing_qr_text ? (
                  <div className="md:col-span-2">
                    <p className="panel-label">{t("agent.whatsappQr")}</p>
                    {whatsappQrSvg ? (
                      <div className="mt-3 flex justify-center border border-[var(--border)] bg-white p-4">
                        <img
                          src={whatsappQrSvg}
                          alt="WhatsApp QR"
                          className="block w-full max-w-[22rem]"
                          style={{ imageRendering: "pixelated" }}
                        />
                      </div>
                    ) : null}
                    <details className="mt-3">
                      <summary className="cursor-pointer text-xs text-[var(--text-secondary)]">
                        {t("agent.whatsappQrAscii")}
                      </summary>
                      <pre className="mt-3 overflow-auto border border-[var(--border)] bg-[var(--surface)] p-4 font-mono text-[10px] leading-[1.05rem] text-[var(--text-primary)]">
                        {whatsappRuntime.pairing_qr_text}
                      </pre>
                    </details>
                  </div>
                ) : null}
                {whatsappRuntime?.session_path ? (
                  <p>
                    <span className="panel-label">{t("agent.whatsappSessionPath")}</span>
                    <br />
                    {whatsappRuntime.session_path}
                  </p>
                ) : null}
                {whatsappRuntime?.bridge_log_path ? (
                  <p>
                    <span className="panel-label">{t("agent.whatsappBridgeLogPath")}</span>
                    <br />
                    {whatsappRuntime.bridge_log_path}
                  </p>
                ) : null}
                <p>{t("agent.whatsappPairingHint")}</p>
              </div>
            </div>
          ) : (
            <div className="mt-5 space-y-3 text-sm leading-6 text-[var(--text-secondary)]">
              <p>
                {t("agent.enabled")}: {whatsapp?.enabled ? t("agent.yes") : t("agent.no")}
              </p>
              <p>
                {t("agent.allowedUsers")}:{" "}
                {whatsapp?.allowed_user_ids?.length ? whatsapp.allowed_user_ids.join(", ") : t("agent.none")}
              </p>
              <p>
                {t("agent.whatsappPairingStatus")}: {whatsappRuntime?.pairing_status ?? t("agent.none")}
              </p>
            </div>
          )}

          <div className="mt-5 border-t border-[var(--border)] pt-4">
            <p className="panel-label">{t("agent.gatewayLogTail")}</p>
            <pre className="mt-3 max-h-60 overflow-auto whitespace-pre-wrap border border-[var(--border)] bg-[var(--surface-raised)] p-4 text-xs leading-6 text-[var(--text-secondary)]">
              {whatsappLogs?.trim() ? whatsappLogs : t("agent.noWhatsappGatewayOutput")}
            </pre>
          </div>
        </section>
      </div>
    </div>
  );
}

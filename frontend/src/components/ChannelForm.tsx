/**
 * ChannelForm — Reusable form for Telegram / WhatsApp messaging channel configuration.
 *
 * Eliminates the ~90% duplicated form markup that previously existed in
 * `AgentMessagingPanel.tsx` for each platform.
 */

import type { Dispatch, SetStateAction } from "react";

import { useMemo } from "react";

import { useI18n } from "../lib/i18n";
import type { MessagingChannelRuntime } from "../types/api";

// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------

export type ChannelFormState = {
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

export const defaultFormState: ChannelFormState = {
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

export type PlatformConfig = {
  /** "telegram" | "whatsapp" */
  platform: string;
  /** Human-readable label shown in the header */
  label: string;
  /** Introductory copy rendered below the label */
  copy: string;
  /** Whether to show the secret-ref selector (Telegram only) */
  showSecretRef: boolean;
  /** Whether to show the WhatsApp-mode selector */
  showWhatsappMode: boolean;
  /** Whether to show the QR / pairing section */
  showQrSection: boolean;
  /** Placeholder for home_chat_id field */
  homeChatIdPlaceholder: string;
  /** i18n key for the enable checkbox label */
  enableLabelKey: string;
  /** i18n key for the save button label */
  saveLabelKey: string;
  /** i18n key for fallback error when gateway is stopped */
  stoppedLabelKey: string;
};

// ---------------------------------------------------------------------------
// Small sub-components
// ---------------------------------------------------------------------------

function ChannelStat({
  label,
  value,
  subtle = false,
}: {
  label: string;
  value: string;
  subtle?: boolean;
}) {
  return (
    <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] px-4 py-3">
      <p className="panel-label">{label}</p>
      <p
        className={`mt-2 break-words text-sm leading-6 ${
          subtle ? "text-[var(--text-secondary)]" : "text-[var(--text-display)]"
        }`}
      >
        {value}
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function ChannelForm({
  config,
  form,
  setForm,
  runtime,
  runtimeStatus,
  lastError,
  secretOptions,
  isAdmin,
  isUpdatePending,
  isStartPending,
  isStopPending,
  qrSvg,
  pairingStatus,
  sessionPath,
  bridgeLogPath,
  pairingQrText,
  onSave,
  onStart,
  onStop,
}: {
  config: PlatformConfig;
  form: ChannelFormState;
  setForm: Dispatch<SetStateAction<ChannelFormState>>;
  runtime: MessagingChannelRuntime | undefined;
  runtimeStatus: string;
  lastError: string | null;
  secretOptions: string[];
  isAdmin: boolean;
  isUpdatePending: boolean;
  isStartPending: boolean;
  isStopPending: boolean;
  qrSvg: string | null;
  pairingStatus: string | null;
  sessionPath: string | null;
  bridgeLogPath: string | null;
  pairingQrText: string | null;
  onSave: () => void;
  onStart: () => void;
  onStop: () => void;
}) {
  const { t } = useI18n();

  const resolvedSecretOptions = useMemo(() => {
    if (form.secret_ref && !secretOptions.includes(form.secret_ref)) {
      return [form.secret_ref, ...secretOptions];
    }
    return secretOptions;
  }, [form.secret_ref, secretOptions]);

  return (
    <section className="panel-frame border border-[var(--border)] p-5">
      {/* Header */}
      <div className="flex items-center justify-between gap-4 border-b border-[var(--border)] pb-4">
        <div>
          <p className="text-lg text-[var(--text-display)]">{config.label}</p>
          <p className="mt-2 text-sm leading-6 text-[var(--text-secondary)]">{config.copy}</p>
        </div>
        <div className="text-right">
          <p className="panel-label">{t("agent.runtime")}</p>
          <p className="mt-2 text-sm uppercase tracking-[0.1em] text-[var(--text-display)]">{runtimeStatus}</p>
          {config.showWhatsappMode && pairingStatus ? (
            <p className="mt-2 text-xs text-[var(--text-secondary)]">
              {t("agent.whatsappPairingStatus")}: {pairingStatus}
            </p>
          ) : runtime?.last_bootstrap_status ? (
            <p className="mt-2 text-xs text-[var(--text-secondary)]">
              {t("agent.bootstrapStatus")}: {runtime.last_bootstrap_status}
            </p>
          ) : null}
        </div>
      </div>

      {/* Stats */}
      <div className="mt-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <ChannelStat label={t("agent.runtime")} value={runtimeStatus} />
        {config.showWhatsappMode ? (
          <>
            <ChannelStat
              label={t("agent.whatsappPairingStatus")}
              value={pairingStatus ?? t("agent.none")}
              subtle={!pairingStatus}
            />
            <ChannelStat
              label={t("agent.allowedUsers")}
              value="—"
              subtle
            />
            <ChannelStat
              label={t("agent.homeChat")}
              value="—"
              subtle
            />
          </>
        ) : (
          <>
            <ChannelStat
              label={t("agent.bootstrapStatus")}
              value={runtime?.last_bootstrap_status ?? t("agent.none")}
              subtle={!runtime?.last_bootstrap_status}
            />
            <ChannelStat
              label={t("agent.allowedUsers")}
              value="—"
              subtle
            />
            <ChannelStat
              label={t("agent.homeChat")}
              value="—"
              subtle
            />
          </>
        )}
      </div>

      {/* Admin form */}
      {isAdmin ? (
        <div className="mt-5 space-y-4">
          {/* Row 1: Secret ref (Telegram) or WhatsApp mode */}
          <div className="grid gap-4 lg:grid-cols-2">
            {config.showSecretRef && (
              <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
                <p className="panel-label">{t("agent.botTokenSecretRef")}</p>
                <label className="panel-field mt-3">
                  <span className="panel-label">{t("agent.botTokenSecretRef")}</span>
                  <select
                    value={form.secret_ref}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, secret_ref: event.target.value }))
                    }
                  >
                    <option value="">
                      {config.platform === "microsoft_teams"
                        ? t("agent.selectTeamsSecret")
                        : config.platform === "google_chat"
                          ? t("agent.selectGoogleChatSecret")
                          : t("agent.selectTelegramSecret")}
                    </option>
                    {resolvedSecretOptions.map((option) => (
                      <option key={option} value={option}>
                        {option}
                      </option>
                    ))}
                  </select>
                </label>
                <p className="mt-3 text-sm leading-6 text-[var(--text-secondary)]">
                  {config.platform === "microsoft_teams"
                    ? t("agent.teamsSecretHint")
                    : config.platform === "google_chat"
                      ? t("agent.googleChatSecretHint")
                      : t("agent.telegramSecretHint")}
                </p>
              </div>
            )}

            {config.showWhatsappMode && (
              <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
                <p className="panel-label">{t("agent.whatsappMode")}</p>
                <label className="panel-field mt-3">
                  <span className="panel-label">{t("agent.whatsappMode")}</span>
                  <select
                    value={form.whatsapp_mode}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, whatsapp_mode: event.target.value }))
                    }
                  >
                    <option value="self-chat">{t("agent.whatsappModeSelfChat")}</option>
                    <option value="bot">{t("agent.whatsappModeBot")}</option>
                  </select>
                </label>
              </div>
            )}

            {/* Allowed users */}
            <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
              <p className="panel-label">
                {config.showSecretRef
                  ? t("agent.allowedTelegramUsers")
                  : t("agent.allowedUsers")}
              </p>
              <label className="panel-field mt-3">
                <span className="panel-label">{t("agent.allowedUsers")}</span>
                <textarea
                  rows={4}
                  value={form.allowed_user_ids}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, allowed_user_ids: event.target.value }))
                  }
                  placeholder={config.showSecretRef ? "123456789\n987654321" : "56912345678@s.whatsapp.net"}
                />
              </label>
            </div>
          </div>

          {/* Home chat */}
          <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
            <p className="panel-label">{t("agent.homeChat")}</p>
            <div className="mt-3 grid gap-4 lg:grid-cols-2">
              <label className="panel-field">
                <span className="panel-label">{t("agent.homeChatId")}</span>
                <input
                  value={form.home_chat_id}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, home_chat_id: event.target.value }))
                  }
                  placeholder={config.homeChatIdPlaceholder}
                />
              </label>
              <label className="panel-field">
                <span className="panel-label">{t("agent.homeChatName")}</span>
                <input
                  value={form.home_chat_name}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, home_chat_name: event.target.value }))
                  }
                  placeholder="Newsroom"
                />
              </label>
              <label className="panel-field lg:col-span-2">
                <span className="panel-label">{t("agent.freeResponseChatIds")}</span>
                <textarea
                  rows={2}
                  value={form.free_response_chat_ids}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, free_response_chat_ids: event.target.value }))
                  }
                  placeholder={config.homeChatIdPlaceholder}
                />
              </label>
            </div>
          </div>

          {/* Behavior settings */}
          <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
            <div className="grid gap-4 lg:grid-cols-2">
              <label className="panel-field">
                <span className="panel-label">{t("agent.unauthorizedDmBehavior")}</span>
                <select
                  value={form.unauthorized_dm_behavior}
                  onChange={(event) =>
                    setForm((current) => ({
                      ...current,
                      unauthorized_dm_behavior: event.target.value,
                    }))
                  }
                >
                  <option value="pair">pair</option>
                  <option value="ignore">ignore</option>
                </select>
              </label>
              <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface)] px-4 py-3">
                <p className="panel-label">{t("agent.mentionGating")}</p>
                <label className="mt-3 flex items-center gap-3 text-sm text-[var(--text-primary)]">
                  <input
                    checked={form.require_mention}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        require_mention: event.target.checked,
                      }))
                    }
                    type="checkbox"
                  />
                  {t("agent.requireMention")}
                </label>
              </div>
            </div>
          </div>

          {/* WhatsApp QR / pairing section */}
          {config.showQrSection && (
            <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
              <p className="panel-label">{t("agent.whatsappPairingStatus")}</p>
              <p className="mt-3 text-sm text-[var(--text-secondary)]">
                {pairingStatus ?? t("agent.none")}
              </p>
              <p className="mt-4 text-xs text-[var(--text-secondary)]">{t("agent.whatsappPairingHint")}</p>

              {(sessionPath || bridgeLogPath) && (
                <div className="mt-4 space-y-2">
                  {sessionPath && (
                    <ChannelStat label={t("agent.whatsappSessionPath")} value={sessionPath} subtle />
                  )}
                  {bridgeLogPath && (
                    <ChannelStat label={t("agent.whatsappBridgeLogPath")} value={bridgeLogPath} subtle />
                  )}
                </div>
              )}

              {pairingQrText && (
                <div className="mt-4">
                  <p className="panel-label">{t("agent.whatsappQr")}</p>
                  {qrSvg ? (
                    <div className="mt-3 inline-block rounded-lg border border-[var(--border)] bg-white p-4">
                      <img src={qrSvg} alt={`${config.label} QR`} className="h-48 w-48" />
                    </div>
                  ) : (
                    <details className="mt-3">
                      <summary className="cursor-pointer text-xs text-[var(--text-secondary)]">
                        {t("agent.whatsappQrAscii")}
                      </summary>
                      <pre className="mt-2 whitespace-pre text-xs leading-4">{pairingQrText}</pre>
                    </details>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Enable + status + actions */}
          <div className="rounded-2xl border border-[var(--border)] bg-[var(--surface-raised)] p-4">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
              <label className="flex items-center gap-3 text-sm text-[var(--text-primary)]">
                <input
                  checked={form.enabled}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, enabled: event.target.checked }))
                  }
                  type="checkbox"
                />
                {t(config.enableLabelKey)}
              </label>
              <p className="panel-inline-status min-w-[14rem] flex-1 lg:text-right">
                {runtime?.status === "running"
                  ? `[LIVE] ${config.platform} gateway pid ${runtime.pid ?? "?"}`
                  : lastError || t(config.stoppedLabelKey)}
              </p>
            </div>
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <button
                type="button"
                className="panel-button-secondary"
                onClick={onSave}
                disabled={isUpdatePending}
              >
                {isUpdatePending ? t("common.loading") : t(config.saveLabelKey)}
              </button>
              <button
                type="button"
                className="panel-button-secondary"
                onClick={onStart}
                disabled={isStartPending}
              >
                {t("agent.startGateway")}
              </button>
              <button
                type="button"
                className="panel-button-secondary"
                onClick={onStop}
                disabled={isStopPending}
              >
                {t("agent.stopGateway")}
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="mt-5 space-y-3 text-sm leading-6 text-[var(--text-secondary)]">
          <p>{t("agent.enabled")}: {form.enabled ? t("agent.yes") : t("agent.no")}</p>
          <p>{t("agent.homeChat")}: {form.home_chat_id || t("agent.none")}</p>
        </div>
      )}
    </section>
  );
}

import { useEffect, useMemo, useState } from "react";

import { useMessagingChannel, useMessagingChannelAction, useMessagingChannelLogs, useMessagingChannelRuntime, useUpdateMessagingChannel } from "../api/messagingChannels";
import { useSecrets } from "../api/secrets";
import { useI18n } from "../lib/i18n";

function parseListInput(value: string) {
  return value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatList(values: string[]) {
  return values.join("\n");
}

export function AgentMessagingPanel({ agentId, isAdmin }: { agentId: string; isAdmin: boolean }) {
  const { t } = useI18n();
  const { data: telegram } = useMessagingChannel(agentId, "telegram");
  const { data: runtime } = useMessagingChannelRuntime(agentId, "telegram");
  const { data: logs } = useMessagingChannelLogs(agentId, "telegram", Boolean(telegram?.enabled));
  const { data: secrets } = useSecrets(isAdmin);
  const updateChannel = useUpdateMessagingChannel();
  const startChannel = useMessagingChannelAction("start");
  const stopChannel = useMessagingChannelAction("stop");
  const [form, setForm] = useState({
    enabled: false,
    secret_ref: "",
    allowed_user_ids: "",
    home_chat_id: "",
    home_chat_name: "",
    require_mention: false,
    free_response_chat_ids: "",
    unauthorized_dm_behavior: "pair",
  });

  useEffect(() => {
    if (!telegram) {
      return;
    }
    setForm({
      enabled: telegram.enabled,
      secret_ref: telegram.secret_ref ?? "",
      allowed_user_ids: formatList(telegram.allowed_user_ids),
      home_chat_id: telegram.home_chat_id ?? "",
      home_chat_name: telegram.home_chat_name ?? "",
      require_mention: telegram.require_mention,
      free_response_chat_ids: formatList(telegram.free_response_chat_ids),
      unauthorized_dm_behavior: telegram.unauthorized_dm_behavior ?? "pair",
    });
  }, [telegram]);

  const secretOptions = useMemo(
    () =>
      (secrets ?? [])
        .map((item) => String(item.name ?? ""))
        .filter(Boolean)
        .sort((left, right) => left.localeCompare(right)),
    [secrets],
  );

  async function onSave() {
    await updateChannel.mutateAsync({
      agentId,
      platform: "telegram",
      payload: {
        enabled: form.enabled,
        secret_ref: form.secret_ref || null,
        allowed_user_ids: parseListInput(form.allowed_user_ids),
        home_chat_id: form.home_chat_id || null,
        home_chat_name: form.home_chat_name || null,
        require_mention: form.require_mention,
        free_response_chat_ids: parseListInput(form.free_response_chat_ids),
        unauthorized_dm_behavior: form.unauthorized_dm_behavior,
      },
    });
  }

  return (
    <div className="mt-6 border-t border-[var(--border)] pt-6">
      <div className="flex items-center justify-between gap-4 border-b border-[var(--border)] pb-4">
        <div>
          <p className="panel-label">{t("agent.messagingChannels")}</p>
          <p className="mt-2 text-lg text-[var(--text-display)]">Telegram</p>
          <p className="mt-2 max-w-[36rem] text-sm leading-6 text-[var(--text-secondary)]">
            {t("agent.telegramCopy")}
          </p>
        </div>
        <div className="text-right">
          <p className="panel-label">{t("agent.runtime")}</p>
          <p className="mt-2 text-sm uppercase tracking-[0.1em] text-[var(--text-display)]">
            {runtime?.status ?? telegram?.status ?? "stopped"}
          </p>
        </div>
      </div>

      {isAdmin ? (
        <div className="mt-5 space-y-4">
          <label className="panel-field">
            <span className="panel-label">{t("agent.botTokenSecretRef")}</span>
            <input
              list={`telegram-secrets-${agentId}`}
              value={form.secret_ref}
              onChange={(event) => setForm((current) => ({ ...current, secret_ref: event.target.value }))}
              placeholder="telegram_bot_secret"
            />
            <datalist id={`telegram-secrets-${agentId}`}>
              {secretOptions.map((option) => (
                <option key={option} value={option} />
              ))}
            </datalist>
          </label>

          <label className="panel-field">
            <span className="panel-label">{t("agent.allowedTelegramUsers")}</span>
            <textarea
              rows={3}
              value={form.allowed_user_ids}
              onChange={(event) => setForm((current) => ({ ...current, allowed_user_ids: event.target.value }))}
              placeholder={"123456789\n987654321"}
            />
          </label>

          <div className="grid gap-4 md:grid-cols-2">
            <label className="panel-field">
              <span className="panel-label">{t("agent.homeChatId")}</span>
              <input
                value={form.home_chat_id}
                onChange={(event) => setForm((current) => ({ ...current, home_chat_id: event.target.value }))}
                placeholder="-1001234567890"
              />
            </label>
            <label className="panel-field">
              <span className="panel-label">{t("agent.homeChatName")}</span>
              <input
                value={form.home_chat_name}
                onChange={(event) => setForm((current) => ({ ...current, home_chat_name: event.target.value }))}
                placeholder="Newsroom"
              />
            </label>
          </div>

          <label className="panel-field">
            <span className="panel-label">{t("agent.freeResponseChatIds")}</span>
            <textarea
              rows={2}
              value={form.free_response_chat_ids}
              onChange={(event) => setForm((current) => ({ ...current, free_response_chat_ids: event.target.value }))}
              placeholder="-1001234567890"
            />
          </label>

          <div className="grid gap-4 md:grid-cols-2">
            <label className="panel-field">
              <span className="panel-label">{t("agent.unauthorizedDmBehavior")}</span>
              <select
                value={form.unauthorized_dm_behavior}
                onChange={(event) =>
                  setForm((current) => ({ ...current, unauthorized_dm_behavior: event.target.value }))
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
                  checked={form.require_mention}
                  onChange={(event) =>
                    setForm((current) => ({ ...current, require_mention: event.target.checked }))
                  }
                  type="checkbox"
                />
                {t("agent.requireMention")}
              </label>
            </label>
          </div>

          <label className="mt-2 flex items-center gap-3 text-sm text-[var(--text-primary)]">
            <input
              checked={form.enabled}
              onChange={(event) => setForm((current) => ({ ...current, enabled: event.target.checked }))}
              type="checkbox"
            />
            {t("agent.enableTelegram")}
          </label>

          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              className="panel-button-secondary"
              onClick={() => void onSave()}
              disabled={updateChannel.isPending}
            >
              {updateChannel.isPending ? t("common.loading") : t("agent.saveTelegram")}
            </button>
            <button
              type="button"
              className="panel-button-secondary"
              onClick={() => startChannel.mutate({ agentId, platform: "telegram" })}
              disabled={startChannel.isPending}
            >
              {t("agent.startGateway")}
            </button>
            <button
              type="button"
              className="panel-button-secondary"
              onClick={() => stopChannel.mutate({ agentId, platform: "telegram" })}
              disabled={stopChannel.isPending}
            >
              {t("agent.stopGateway")}
            </button>
            <p className="panel-inline-status">
              {runtime?.status === "running"
                ? `[LIVE] telegram gateway pid ${runtime.pid ?? "?"}`
                : telegram?.last_error || t("agent.gatewayStopped")}
            </p>
          </div>
        </div>
      ) : (
        <div className="mt-5 space-y-3 text-sm leading-6 text-[var(--text-secondary)]">
          <p>{t("agent.enabled")}: {telegram?.enabled ? t("agent.yes") : t("agent.no")}</p>
          <p>{t("agent.allowedUsers")}: {telegram?.allowed_user_ids?.length ? telegram.allowed_user_ids.join(", ") : t("agent.none")}</p>
          <p>{t("agent.homeChat")}: {telegram?.home_chat_id ?? t("agent.none")}</p>
        </div>
      )}

      <div className="mt-5 border-t border-[var(--border)] pt-4">
        <p className="panel-label">{t("agent.gatewayLogTail")}</p>
        <pre className="mt-3 max-h-60 overflow-auto whitespace-pre-wrap border border-[var(--border)] bg-[var(--surface-raised)] p-4 text-xs leading-6 text-[var(--text-secondary)]">
          {logs?.trim() ? logs : t("agent.noGatewayOutput")}
        </pre>
      </div>
    </div>
  );
}

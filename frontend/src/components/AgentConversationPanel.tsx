import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

import { useI18n } from "../lib/i18n";
import type { Task } from "../types/api";

type ConversationEntry = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: string;
  status: string;
  title?: string | null;
};

function buildAssistantContent(task: Task) {
  if (task.response?.trim()) {
    return task.response.trim();
  }
  const streamed = task.messages_json
    .filter((message) => message.role === "assistant")
    .map((message) => message.content)
    .join("")
    .trim();
  if (streamed) {
    return streamed;
  }
  if (task.status === "failed" && task.error_message) {
    return task.error_message;
  }
  if (task.status === "queued") {
    return "__QUEUED__";
  }
  if (task.status === "running") {
    return "__RUNNING__";
  }
  return "";
}

function statusTone(status: string) {
  if (status === "completed") return "text-[var(--success)]";
  if (status === "running" || status === "queued") return "text-[var(--warning)]";
  if (status === "failed") return "text-[var(--accent)]";
  return "text-[var(--text-secondary)]";
}

export function AgentConversationPanel({
  tasks,
  agentStatus,
  onSubmit,
  isSubmitting,
  disabled = false,
  embedded = false,
}: {
  tasks: Task[];
  agentStatus: string;
  onSubmit: (prompt: string) => Promise<void>;
  isSubmitting: boolean;
  disabled?: boolean;
  embedded?: boolean;
}) {
  const { t, formatDateTime } = useI18n();
  const [draftPrompt, setDraftPrompt] = useState("");
  const feedRef = useRef<HTMLDivElement | null>(null);

  const entries = useMemo(() => {
    const sortedTasks = [...tasks].sort(
      (left, right) => new Date(left.queued_at).getTime() - new Date(right.queued_at).getTime(),
    );

    return sortedTasks.flatMap((task) => {
      const items: ConversationEntry[] = [
        {
          id: `${task.id}-user`,
          role: "user",
          content: task.prompt,
          timestamp: task.queued_at,
          status: task.status,
          title: task.title,
        },
      ];

      const assistantContent = buildAssistantContent(task);
      if (assistantContent) {
        items.push({
          id: `${task.id}-assistant`,
          role: task.status === "failed" ? "system" : "assistant",
          content: assistantContent,
          timestamp: task.completed_at ?? task.started_at ?? task.queued_at,
          status: task.status,
          title: task.title,
        });
      }

      return items;
    });
  }, [tasks]);

  useEffect(() => {
    const node = feedRef.current;
    if (!node) {
      return;
    }
    node.scrollTop = node.scrollHeight;
  }, [entries.length]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (disabled || !draftPrompt.trim()) {
      return;
    }
    await onSubmit(draftPrompt.trim());
    setDraftPrompt("");
  }

  return (
    <section className={embedded ? "" : "panel-frame p-6"}>
      {embedded ? null : (
        <div className="flex items-end justify-between gap-4 border-b border-[var(--border)] pb-4">
          <div>
            <p className="panel-label">{t("agent.conversation")}</p>
            <h3 className="mt-2 text-2xl text-[var(--text-display)]">{t("agent.talkToAgent")}</h3>
          </div>
          <p className="panel-label">
            {agentStatus === "running" ? t("agent.liveRuntime") : t("agent.autoStartOnSend")}
          </p>
        </div>
      )}

      <div
        ref={feedRef}
        className={`${embedded ? "mt-0" : "mt-6"} max-h-[560px] space-y-4 overflow-y-auto border border-[var(--border)] p-4`}
        style={{ background: "color-mix(in srgb, var(--surface) 60%, transparent)" }}
      >
        {entries.length ? (
          entries.map((entry) => {
            const isUser = entry.role === "user";
            const isSystem = entry.role === "system";
            return (
              <article
                key={entry.id}
                className={`max-w-[88%] border px-4 py-3 ${
                  isUser
                    ? "ml-auto border-[var(--text-display)] bg-[var(--text-display)] text-[var(--black)]"
                    : isSystem
                      ? "border-[var(--accent)] bg-[var(--accent-subtle)] text-[var(--text-primary)]"
                      : "border-[var(--border-visible)] bg-[var(--surface-raised)] text-[var(--text-primary)]"
                }`}
              >
                <div className="flex items-center justify-between gap-4">
                  <p className={`panel-label ${isUser ? "[color:var(--contrast-muted)]" : ""}`}>
                    {isUser ? t("agent.operator") : isSystem ? t("agent.runtimeError") : "Agent"}
                  </p>
                  <p className={`text-xs uppercase tracking-[0.1em] ${isUser ? "[color:var(--contrast-muted)]" : statusTone(entry.status)}`}>
                    {entry.status}
                  </p>
                </div>
                {entry.title ? (
                  <p className={`mt-2 text-xs uppercase tracking-[0.1em] ${isUser ? "[color:var(--contrast-muted)]" : "text-[var(--text-disabled)]"}`}>
                    {entry.title}
                  </p>
                ) : null}
                <p className="mt-3 whitespace-pre-wrap text-sm leading-6">
                  {entry.content === "__QUEUED__"
                    ? t("agent.queuedWaiting")
                    : entry.content === "__RUNNING__"
                      ? t("agent.running")
                      : entry.content}
                </p>
                <p className={`mt-3 text-xs uppercase tracking-[0.1em] ${isUser ? "[color:var(--contrast-muted)]" : "text-[var(--text-disabled)]"}`}>
                  {formatDateTime(entry.timestamp)}
                </p>
              </article>
            );
          })
        ) : (
          <p className="panel-inline-status">
            {t("agent.emptyConversation")}
          </p>
        )}
      </div>

      <form className="mt-5 space-y-4" onSubmit={handleSubmit}>
        <label className="panel-field">
          <span className="panel-label">{t("agent.message")}</span>
          <textarea
            rows={4}
            value={draftPrompt}
            onChange={(event) => setDraftPrompt(event.target.value)}
            placeholder={t("agent.messagePlaceholder")}
            disabled={disabled}
          />
        </label>

        <div className="flex flex-wrap items-center gap-3">
          <button
            type="submit"
            className="panel-button-primary"
            disabled={disabled || isSubmitting || !draftPrompt.trim()}
          >
            {isSubmitting ? t("common.loading") : t("agent.sendMessage")}
          </button>
          <p className="panel-inline-status">
            {disabled
              ? t("agent.archivedConversationDisabled")
              : agentStatus === "running"
              ? t("agent.liveDispatch")
              : t("agent.autoDispatch")}
          </p>
        </div>
      </form>
    </section>
  );
}
